from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from textbot.config import Settings
from textbot.models import Decision, InboundText, ThreadMessage
from textbot.pipeline import Pipeline
from textbot.store import Store

TENANT = "3345550101"
PARK_LINE = "2565550199"
# 2pm Central on a weekday: outside quiet hours
NOON_CT = datetime(2026, 10, 7, 19, 0, tzinfo=timezone.utc)


class FakeRC:
    configured = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.thread: list[ThreadMessage] = []
        self.fail_send = False

    async def get_thread(self, conversation_id: str, lookback_days: int) -> list[ThreadMessage]:
        return list(self.thread)

    async def send_sms(self, from_phone: str, to_phone: str, text: str) -> str:
        if self.fail_send:
            raise RuntimeError("RingCentral down")
        self.sent.append((from_phone, to_phone, text))
        return "sms-1"

    async def ensure_subscription(self, address: str, token: str) -> str:
        return "sub-1"


class FakeAgent:
    def __init__(self, decision: Decision | None = None) -> None:
        self.decision = decision or safe_decision()
        self.calls = 0
        self.error: Exception | None = None

    async def decide(self, thread, tenant_phone, now) -> Decision:
        self.calls += 1
        if self.error:
            raise self.error
        return dataclasses.replace(self.decision)


class FakeSlack:
    def __init__(self) -> None:
        self.posts: list[tuple[str, list | None]] = []
        self.updates: list[tuple[str, str]] = []
        self.modals: list[dict] = []

    async def post(self, text: str, blocks=None) -> str:
        self.posts.append((text, blocks))
        return f"ts-{len(self.posts)}"

    async def update(self, ts: str, text: str, blocks=None) -> None:
        self.updates.append((ts, text))

    async def open_modal(self, trigger_id: str, view: dict) -> None:
        self.modals.append(view)


def safe_decision(**overrides) -> Decision:
    base = dict(
        category="maintenance",
        action="send",
        reply="You can submit it here: https://stratexmhp.com/submit-request/aspen-ridge-capital",
        reason="Maintenance request; sending the self-service link.",
        tenant_verified=True,
        all_facts_verified=True,
        creates_new_commitment=False,
        sensitive_topic=False,
        lookups_done=[],
        needs_from_jeff="",
    )
    base.update(overrides)
    return Decision(**base)


def make_settings(**overrides) -> Settings:
    base = dict(shadow_mode=True, debounce_seconds=0, db_path=":memory:")
    base.update(overrides)
    return Settings(**base)


def inbound(message_id: str = "m1", text: str = "my sink is leaking", conv: str = "c1") -> InboundText:
    return InboundText(
        message_id=message_id,
        conversation_id=conv,
        tenant_phone=TENANT,
        our_phone=PARK_LINE,
        text=text,
        created=NOON_CT,
    )


@pytest.fixture
def make_pipeline():
    def _make(settings: Settings | None = None, decision: Decision | None = None):
        settings = settings or make_settings()
        pipe = Pipeline(
            settings,
            Store(settings.db_path),
            FakeRC(),
            FakeAgent(decision),
            FakeSlack(),
            clock=lambda: NOON_CT,
        )

        async def no_sleep(_seconds):
            return None

        pipe._sleep = no_sleep
        return pipe

    return _make
