import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from textbot.main import create_app

from tests.conftest import TENANT, make_settings

SECRET = "slack-secret"


def client_for(make_pipeline, **settings):
    s = make_settings(slack_signing_secret=SECRET, rc_webhook_verification_token="rc-token", **settings)
    pipe = make_pipeline(s)
    return TestClient(create_app(s, pipe)), pipe


def slack_headers(body: bytes) -> dict:
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(SECRET.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": sig,
        "Content-Type": "application/x-www-form-urlencoded",
    }


def notification(message_id="m1", text="sink is leaking"):
    return {
        "event": "/restapi/v1.0/account/~/extension/~/message-store/instant?type=SMS",
        "body": {
            "id": message_id,
            "conversationId": 555,
            "direction": "Inbound",
            "type": "SMS",
            "from": {"phoneNumber": f"+1{TENANT}"},
            "to": [{"phoneNumber": "+12565550199"}],
            "subject": text,
            "creationTime": "2026-10-07T19:00:00.000Z",
        },
    }


def test_ringcentral_validation_handshake(make_pipeline):
    client, _ = client_for(make_pipeline)
    resp = client.post("/webhooks/ringcentral", headers={"Validation-Token": "abc"})
    assert resp.status_code == 200
    assert resp.headers["Validation-Token"] == "abc"


def test_ringcentral_rejects_wrong_verification_token(make_pipeline):
    client, pipe = client_for(make_pipeline)
    resp = client.post("/webhooks/ringcentral", json=notification(), headers={"Verification-Token": "nope"})
    assert resp.status_code == 403
    assert pipe.agent.calls == 0


def test_ringcentral_inbound_sms_creates_draft(make_pipeline):
    client, pipe = client_for(make_pipeline)
    resp = client.post("/webhooks/ringcentral", json=notification(), headers={"Verification-Token": "rc-token"})
    assert resp.status_code == 200
    assert pipe.agent.calls == 1
    assert pipe.store.get_draft(1).inbound_text == "sink is leaking"


def test_ringcentral_ignores_outbound(make_pipeline):
    client, pipe = client_for(make_pipeline)
    note = notification()
    note["body"]["direction"] = "Outbound"
    client.post("/webhooks/ringcentral", json=note, headers={"Verification-Token": "rc-token"})
    assert pipe.agent.calls == 0


def test_slack_rejects_bad_signature(make_pipeline):
    client, _ = client_for(make_pipeline)
    body = urlencode({"payload": "{}"}).encode()
    headers = slack_headers(body) | {"X-Slack-Signature": "v0=bad"}
    assert client.post("/slack/interactions", content=body, headers=headers).status_code == 401


def test_slack_send_button_sends(make_pipeline):
    client, pipe = client_for(make_pipeline)
    client.post("/webhooks/ringcentral", json=notification(), headers={"Verification-Token": "rc-token"})
    payload = {"type": "block_actions", "user": {"id": "U1"}, "actions": [{"action_id": "send", "value": "1"}]}
    body = urlencode({"payload": json.dumps(payload)}).encode()
    assert client.post("/slack/interactions", content=body, headers=slack_headers(body)).status_code == 200
    assert len(pipe.rc.sent) == 1


def test_slack_approver_allowlist(make_pipeline):
    client, pipe = client_for(make_pipeline, slack_approver_user_ids=frozenset({"UJEFF"}))
    client.post("/webhooks/ringcentral", json=notification(), headers={"Verification-Token": "rc-token"})
    payload = {"type": "block_actions", "user": {"id": "USOMEONE"}, "actions": [{"action_id": "send", "value": "1"}]}
    body = urlencode({"payload": json.dumps(payload)}).encode()
    client.post("/slack/interactions", content=body, headers=slack_headers(body))
    assert pipe.rc.sent == []


def test_slack_edit_modal_and_submission(make_pipeline):
    client, pipe = client_for(make_pipeline)
    client.post("/webhooks/ringcentral", json=notification(), headers={"Verification-Token": "rc-token"})
    payload = {"type": "block_actions", "user": {"id": "U1"}, "trigger_id": "t", "actions": [{"action_id": "edit", "value": "1"}]}
    body = urlencode({"payload": json.dumps(payload)}).encode()
    client.post("/slack/interactions", content=body, headers=slack_headers(body))
    assert pipe.slack.modals[0]["private_metadata"] == "1"

    submission = {
        "type": "view_submission",
        "user": {"id": "U1"},
        "view": {
            "callback_id": "edit_reply",
            "private_metadata": "1",
            "state": {"values": {"reply": {"text": {"value": "Edited text"}}}},
        },
    }
    body = urlencode({"payload": json.dumps(submission)}).encode()
    client.post("/slack/interactions", content=body, headers=slack_headers(body))
    assert pipe.rc.sent[0][2] == "Edited text"


def test_textbot_pause_command(make_pipeline):
    client, pipe = client_for(make_pipeline)
    body = urlencode({"command": "/textbot", "text": "pause", "user_id": "U1"}).encode()
    resp = client.post("/slack/commands", content=body, headers=slack_headers(body))
    assert "paused" in resp.text
    assert pipe.store.is_paused()
    assert client.get("/health").json()["paused"] is True


def test_ringcentral_rejects_everything_when_no_token_configured(make_pipeline):
    s = make_settings(slack_signing_secret=SECRET)
    pipe = make_pipeline(s)
    client = TestClient(create_app(s, pipe))
    assert client.post("/webhooks/ringcentral", json=notification()).status_code == 403
    assert pipe.agent.calls == 0
