"""Slack: approval cards with Send / Edit / Skip, notices, and request verification."""

from __future__ import annotations

import hashlib
import hmac
import time

import httpx2 as httpx

from textbot.phone import display_phone
from textbot.store import Draft

SLACK_API = "https://slack.com/api"
MAX_SKEW_SECONDS = 60 * 5


def verify_signature(
    signing_secret: str, timestamp: str, body: bytes, signature: str, now: float | None = None
) -> bool:
    if not (signing_secret and timestamp and signature):
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > MAX_SKEW_SECONDS:
        return False
    base = f"v0:{timestamp}:".encode() + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in (text or "(empty)").splitlines() or ["(empty)"])


def draft_blocks(draft: Draft, header: str, *, buttons: bool) -> list[dict]:
    lines = [
        f"*{header}*  ·  {display_phone(draft.tenant_phone)}  ·  _{draft.category}_",
        f"*They wrote:*\n{_quote(draft.inbound_text)}",
    ]
    if draft.reply:
        lines.append(f"*Proposed reply:*\n{_quote(draft.reply)}")
    lines.append(f"*Claude's reasoning:* {draft.reason}")
    if draft.blockers:
        lines.append("*Held because:* " + "; ".join(draft.blockers))
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n\n".join(lines)[:2900]}}
    ]
    if buttons:
        actions = []
        if draft.reply:
            actions.append(
                {
                    "type": "button",
                    "action_id": "send",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Send"},
                    "value": str(draft.id),
                }
            )
        actions.append(
            {
                "type": "button",
                "action_id": "edit",
                "text": {"type": "plain_text", "text": "Edit" if draft.reply else "Write reply"},
                "value": str(draft.id),
            }
        )
        actions.append(
            {
                "type": "button",
                "action_id": "skip",
                "text": {"type": "plain_text", "text": "Skip"},
                "value": str(draft.id),
            }
        )
        blocks.append({"type": "actions", "elements": actions})
    return blocks


def edit_modal(draft: Draft) -> dict:
    return {
        "type": "modal",
        "callback_id": "edit_reply",
        "private_metadata": str(draft.id),
        "title": {"type": "plain_text", "text": "Edit reply"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"They wrote: {draft.inbound_text[:2000]}"}],
            },
            {
                "type": "input",
                "block_id": "reply",
                "label": {"type": "plain_text", "text": "Reply"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "text",
                    "multiline": True,
                    "initial_value": draft.reply,
                },
            },
        ],
    }


class SlackClient:
    def __init__(self, bot_token: str, channel_id: str) -> None:
        self._channel = channel_id
        self._http = httpx.AsyncClient(
            base_url=SLACK_API,
            timeout=20.0,
            headers={"Authorization": f"Bearer {bot_token}"},
        )

    async def _call(self, method: str, payload: dict) -> dict:
        resp = await self._http.post(f"/{method}", json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack {method} failed: {data.get('error')}")
        return data

    async def post(self, text: str, blocks: list[dict] | None = None) -> str:
        payload: dict = {"channel": self._channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
        return (await self._call("chat.postMessage", payload))["ts"]

    async def update(self, ts: str, text: str, blocks: list[dict] | None = None) -> None:
        payload: dict = {"channel": self._channel, "ts": ts, "text": text}
        payload["blocks"] = blocks or []
        await self._call("chat.update", payload)

    async def open_modal(self, trigger_id: str, view: dict) -> None:
        await self._call("views.open", {"trigger_id": trigger_id, "view": view})

    async def aclose(self) -> None:
        await self._http.aclose()
