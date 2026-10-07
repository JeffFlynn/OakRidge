"""Ask Claude what to do with a conversation, using the reply rules and read-only lookups."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import anthropic

from textbot.models import ACTIONS, CATEGORIES, Decision, ThreadMessage
from textbot.phone import display_phone

log = logging.getLogger(__name__)

RULES = (Path(__file__).parent / "prompts" / "reply_rules.md").read_text()
MAX_TOOL_ROUNDS = 6

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "action": {"type": "string", "enum": list(ACTIONS)},
        "reply": {"type": "string"},
        "reason": {"type": "string"},
        "tenant_verified": {"type": "boolean"},
        "all_facts_verified": {"type": "boolean"},
        "creates_new_commitment": {"type": "boolean"},
        "sensitive_topic": {"type": "boolean"},
        "lookups_done": {"type": "array", "items": {"type": "string"}},
        "needs_from_jeff": {"type": "string"},
    },
    "required": [
        "category",
        "action",
        "reply",
        "reason",
        "tenant_verified",
        "all_facts_verified",
        "creates_new_commitment",
        "sensitive_topic",
        "lookups_done",
        "needs_from_jeff",
    ],
    "additionalProperties": False,
}

TOOLS = [
    {
        "name": "find_tenant_by_phone",
        "description": (
            "Look up which Rent Manager tenant(s) have this phone number on file. Returns "
            "tenant_id, name, status, property, unit, balance and phones for each match. "
            "Use it to verify who is texting before discussing their account."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"phone": {"type": "string", "description": "10-digit US phone number"}},
            "required": ["phone"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_tenant_account",
        "description": (
            "Get one tenant's current balance and most recent transactions (charges and "
            "payments, newest first) from Rent Manager. Use before saying anything about money."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"tenant_id": {"type": "integer"}},
            "required": ["tenant_id"],
            "additionalProperties": False,
        },
    },
]


class TenantLookup(Protocol):
    configured: bool

    async def find_tenants_by_phone(self, phone: str) -> list[dict]: ...

    async def get_account(self, tenant_id: int) -> dict: ...


def format_thread(
    thread: list[ThreadMessage], tenant_phone: str, now: datetime, tz: str
) -> str:
    zone = ZoneInfo(tz)
    lines = []
    for m in thread:
        who = "TEXTER" if m.direction == "Inbound" else "PARK"
        stamp = m.created.astimezone(zone).strftime("%a %m/%d %I:%M %p")
        lines.append(f"[{stamp}] {who}: {m.text}")
    return (
        f"Current time: {now.astimezone(zone).strftime('%A %m/%d/%Y %I:%M %p %Z')}\n"
        f"Texter's phone: {display_phone(tenant_phone)} ({tenant_phone})\n\n"
        "<conversation>\n" + "\n".join(lines) + "\n</conversation>\n\n"
        "The last TEXTER message(s) are new. Decide what should happen next."
    )


class ReplyAgent:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        lookup: TenantLookup,
        model: str,
        effort: str,
        timezone: str,
    ) -> None:
        self._client = client
        self._lookup = lookup
        self._model = model
        self._effort = effort
        self._tz = timezone

    async def _run_tool(self, name: str, args: dict) -> tuple[str, bool]:
        if not self._lookup.configured:
            return "Rent Manager is not connected yet. No account data is available.", True
        try:
            if name == "find_tenant_by_phone":
                result = await self._lookup.find_tenants_by_phone(str(args["phone"]))
                return json.dumps({"matches": result}, default=str), False
            if name == "get_tenant_account":
                result = await self._lookup.get_account(int(args["tenant_id"]))
                return json.dumps(result, default=str), False
            return f"Unknown tool {name}", True
        except Exception as exc:  # report lookup failures to Claude instead of crashing
            log.exception("tool %s failed", name)
            return f"Lookup failed: {exc}", True

    async def decide(
        self, thread: list[ThreadMessage], tenant_phone: str, now: datetime
    ) -> Decision:
        messages: list[dict] = [
            {"role": "user", "content": format_thread(thread, tenant_phone, now, self._tz)}
        ]
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._client.beta.messages.create(
                model=self._model,
                max_tokens=16000,
                system=[{"type": "text", "text": RULES, "cache_control": {"type": "ephemeral"}}],
                tools=TOOLS,
                messages=messages,
                output_config={
                    "effort": self._effort,
                    "format": {"type": "json_schema", "schema": DECISION_SCHEMA},
                },
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if response.stop_reason == "refusal":
                return Decision.hold("Claude declined to handle this thread; needs a person.")
            if response.stop_reason == "max_tokens":
                return Decision.hold("Claude's answer was cut off; needs a person.")

            # Append the full content (thinking blocks included) so the history stays intact.
            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason == "tool_use" and tool_uses:
                results = []
                for block in tool_uses:
                    content, is_error = await self._run_tool(block.name, dict(block.input))
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": content,
                            "is_error": is_error,
                        }
                    )
                messages.append({"role": "user", "content": results})
                continue

            text = next((b.text for b in response.content if b.type == "text"), "")
            return parse_decision(text)
        return Decision.hold("Claude needed too many lookups; needs a person.")


def parse_decision(text: str) -> Decision:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return Decision.hold("Claude's answer wasn't valid JSON; needs a person.")
    if data.get("category") not in CATEGORIES or data.get("action") not in ACTIONS:
        return Decision.hold("Claude's answer was malformed; needs a person.")
    return Decision(
        category=data["category"],
        action=data["action"],
        reply=str(data.get("reply", "")),
        reason=str(data.get("reason", "")),
        tenant_verified=bool(data.get("tenant_verified")),
        all_facts_verified=bool(data.get("all_facts_verified")),
        creates_new_commitment=bool(data.get("creates_new_commitment")),
        sensitive_topic=bool(data.get("sensitive_topic")),
        lookups_done=[str(x) for x in data.get("lookups_done", [])],
        needs_from_jeff=str(data.get("needs_from_jeff", "")),
    )
