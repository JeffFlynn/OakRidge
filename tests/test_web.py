import asyncio

import pytest
from fastapi.testclient import TestClient

from textbot import auth
from textbot.main import create_app

from tests.conftest import TENANT, inbound, make_settings, safe_decision

HOST = "testserver"
SAME_SITE = {"Origin": f"http://{HOST}"}
PASSWORD = "correct-horse-battery"


def notification(message_id="m1", text="sink is leaking", direction="Inbound"):
    return {
        "event": "/restapi/v1.0/account/~/extension/~/message-store/instant?type=SMS",
        "body": {
            "id": message_id,
            "conversationId": 555,
            "direction": direction,
            "type": "SMS",
            "from": {"phoneNumber": f"+1{TENANT}"},
            "to": [{"phoneNumber": "+12565550199"}],
            "subject": text,
            "creationTime": "2026-10-07T19:00:00.000Z",
        },
    }


@pytest.fixture
def app_for(make_pipeline):
    def _make(**settings):
        s = make_settings(
            rc_webhook_verification_token="rc-token",
            session_secret="test-secret",
            admin_username="jeff",
            admin_password=PASSWORD,
            **settings,
        )
        pipe = make_pipeline(s)
        return TestClient(create_app(s, pipe)), pipe

    return _make


def login(client, username="jeff", password=PASSWORD):
    return client.post(
        "/login", data={"username": username, "password": password}, headers=SAME_SITE, follow_redirects=False
    )


# RingCentral webhook


def test_ringcentral_validation_handshake(app_for):
    client, _ = app_for()
    resp = client.post("/webhooks/ringcentral", headers={"Validation-Token": "abc"})
    assert resp.status_code == 200 and resp.headers["Validation-Token"] == "abc"


def test_ringcentral_rejects_wrong_or_missing_token(app_for):
    client, pipe = app_for()
    assert client.post("/webhooks/ringcentral", json=notification(), headers={"Verification-Token": "x"}).status_code == 403
    assert client.post("/webhooks/ringcentral", json=notification()).status_code == 403
    assert pipe.agent.calls == 0


def test_ringcentral_inbound_sms_creates_draft(app_for):
    client, pipe = app_for()
    resp = client.post("/webhooks/ringcentral", json=notification(), headers={"Verification-Token": "rc-token"})
    assert resp.status_code == 200
    assert pipe.store.get_draft(1).inbound_text == "sink is leaking"


def test_ringcentral_outbound_retires_pending(app_for):
    client, pipe = app_for()
    client.post("/webhooks/ringcentral", json=notification(), headers={"Verification-Token": "rc-token"})
    client.post("/webhooks/ringcentral", json=notification("m9", "on it", "Outbound"), headers={"Verification-Token": "rc-token"})
    assert pipe.store.get_draft(1).status == "superseded"


# Login and sessions


def test_pages_require_login(app_for):
    client, _ = app_for()
    for path in ("/", "/history", "/scorecard", "/settings"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/login", path


def test_bootstrap_admin_can_log_in(app_for):
    client, _ = app_for()
    resp = login(client)
    assert resp.status_code == 303
    assert "httponly" in resp.headers["set-cookie"].lower()
    assert client.get("/").status_code == 200


def test_wrong_password_and_throttle(app_for):
    client, _ = app_for()
    for _ in range(5):
        assert "Wrong username or password" in login(client, password="nope-nope-nope").text
    assert "Too many attempts" in login(client).text


def test_login_rejects_cross_site_post(app_for):
    client, _ = app_for()
    resp = client.post("/login", data={"username": "jeff", "password": PASSWORD}, headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_tampered_cookie_is_rejected(app_for):
    client, _ = app_for()
    login(client)
    cookie = client.cookies.get(auth.SESSION_COOKIE)
    client.cookies.set(auth.SESSION_COOKIE, cookie[:-1] + ("0" if cookie[-1] != "0" else "1"))
    assert client.get("/", follow_redirects=False).status_code == 303


def test_password_change_signs_out_other_sessions(app_for):
    client, _ = app_for()
    other, _ = TestClient(client.app), None
    login(client)
    login(other)
    resp = client.post("/account/password", data={"current": PASSWORD, "new": "a-brand-new-password"}, headers=SAME_SITE)
    assert "Password changed" in resp.text
    assert client.get("/", follow_redirects=False).status_code == 200
    assert other.get("/", follow_redirects=False).status_code == 303


# Inbox


def test_inbox_shows_draft_and_send_works(app_for):
    client, pipe = app_for()
    asyncio.run(pipe.handle_inbound(inbound()))
    login(client)
    page = client.get("/")
    assert "my sink is leaking" in page.text
    assert "stratexmhp.com/submit-request" in page.text
    resp = client.post("/drafts/1/send", data={"edited": "1", "text": "Sending a plumber today."}, headers=SAME_SITE)
    assert "Sent." in resp.text
    assert pipe.rc.sent[-1][2] == "Sending a plumber today."
    assert pipe.store.get_draft(1).decided_by == "jeff"


def test_inbox_escapes_tenant_text(app_for):
    client, pipe = app_for()
    asyncio.run(pipe.handle_inbound(inbound(text="<script>alert(1)</script>")))
    login(client)
    page = client.get("/").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_send_requires_same_origin(app_for):
    client, pipe = app_for()
    asyncio.run(pipe.handle_inbound(inbound()))
    login(client)
    resp = client.post("/drafts/1/send", data={"edited": "1", "text": "hi"}, headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
    assert pipe.rc.sent == []


def test_skip_from_inbox(app_for):
    client, pipe = app_for()
    asyncio.run(pipe.handle_inbound(inbound()))
    login(client)
    assert "Skipped." in client.post("/drafts/1/skip", headers=SAME_SITE).text
    assert pipe.store.get_draft(1).status == "skipped"


def test_history_and_scorecard(app_for):
    client, pipe = app_for()
    asyncio.run(pipe.handle_inbound(inbound()))
    asyncio.run(pipe.approve(1, None, "jeff"))
    login(client)
    assert "my sink is leaking" in client.get("/history?q=5550101").text
    card = client.get("/scorecard").text
    assert "Maintenance" in card and "100%" in card


# Settings and roles


def test_admin_changes_settings(app_for):
    client, pipe = app_for()
    login(client)
    resp = client.post(
        "/settings/bot",
        data={"auto_send_categories": ["maintenance", "ada", "bogus"]},
        headers=SAME_SITE,
    )
    assert "Settings saved" in resp.text
    effective = pipe.effective_settings()
    assert effective.shadow_mode is False
    assert effective.auto_send_categories == frozenset({"maintenance"})  # ada and junk ignored
    assert not pipe.store.is_paused()


def test_staff_user_cannot_change_settings_but_can_pause(app_for):
    client, pipe = app_for()
    pipe.store.add_user("kaori", auth.hash_password("kaori-password-1"), is_admin=False)
    login(client, "kaori", "kaori-password-1")
    assert client.post("/settings/bot", data={}, headers=SAME_SITE).status_code == 403
    assert client.post("/settings/users", data={"username": "x", "password": "y" * 12}, headers=SAME_SITE).status_code == 403
    assert "Bot paused" in client.post("/settings/pause", headers=SAME_SITE).text
    assert pipe.store.is_paused()


def test_admin_adds_and_disables_user(app_for):
    client, pipe = app_for()
    login(client)
    client.post("/settings/users", data={"username": "herlen", "password": "herlen-password"}, headers=SAME_SITE)
    herlen = pipe.store.get_user_by_name("herlen")
    assert herlen and not herlen.is_admin
    staff = TestClient(client.app)
    login(staff, "herlen", "herlen-password")
    assert staff.get("/", follow_redirects=False).status_code == 200
    client.post(f"/settings/users/{herlen.id}/toggle", headers=SAME_SITE)
    assert staff.get("/", follow_redirects=False).status_code == 303


def test_short_passwords_rejected(app_for):
    client, pipe = app_for()
    login(client)
    resp = client.post("/settings/users", data={"username": "x", "password": "short"}, headers=SAME_SITE)
    assert "10+ characters" in resp.text
    assert pipe.store.get_user_by_name("x") is None
