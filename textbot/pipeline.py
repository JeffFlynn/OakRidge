"""What happens to an inbound text, and to approvals from the web inbox."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from textbot import store as st
from textbot.config import Settings
from textbot.models import Decision, InboundText, ThreadMessage
from textbot.phone import display_phone
from textbot.policy import PolicyContext, evaluate, in_quiet_hours

log = logging.getLogger(__name__)

PAUSE_COMMANDS = {"PAUSE BOT", "PAUSE"}


def _local_midnight_utc(now: datetime, tz: str) -> datetime:
    local = now.astimezone(ZoneInfo(tz))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _thread_snapshot(thread: list[ThreadMessage]) -> list[dict]:
    return [
        {"direction": m.direction, "text": m.text, "created": m.created.isoformat()}
        for m in thread[-30:]
    ]


class Pipeline:
    def __init__(self, settings: Settings, store: st.Store, rc, agent, alerter, clock=None) -> None:
        self.settings = settings
        self.store = store
        self.rc = rc
        self.agent = agent
        self.alerter = alerter
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = asyncio.sleep

    def effective_settings(self) -> Settings:
        """Env defaults, overridden by anything changed on the Settings page."""
        s = self.settings
        shadow = self.store.get_setting("shadow_mode")
        cats = self.store.get_setting("auto_send_categories")
        return dataclasses.replace(
            s,
            shadow_mode=s.shadow_mode if shadow is None else shadow == "1",
            auto_send_categories=(
                s.auto_send_categories
                if cats is None
                else frozenset(c for c in cats.split(",") if c)
            ),
        )

    # Inbound texts

    async def handle_inbound(self, msg: InboundText) -> None:
        """Entry point for each inbound SMS notification (runs in the background)."""
        is_new = self.store.record_inbound(
            msg.message_id,
            msg.conversation_id,
            msg.tenant_phone,
            msg.our_phone,
            msg.text,
            msg.created.isoformat(),
        )
        if not is_new:
            return  # RingCentral sometimes delivers the same notification twice
        try:
            await self._process(msg)
        except Exception as exc:
            log.exception("failed to process message %s", msg.message_id)
            await self._problem(
                f"Couldn't process a text from {display_phone(msg.tenant_phone)} "
                f"({type(exc).__name__}). Please handle it in RingCentral."
            )

    async def _process(self, msg: InboundText) -> None:
        settings = self.effective_settings()
        if msg.tenant_phone in settings.alert_numbers:
            # Phone kill switch: Jeff texts "PAUSE BOT" to any park line.
            if msg.text.strip().upper() in PAUSE_COMMANDS:
                self.store.set_setting("paused", "1")
                self.store.log_event("info", f"Paused by text from {display_phone(msg.tenant_phone)}")
                await self.rc.send_sms(
                    msg.our_phone, msg.tenant_phone, "Text bot paused. Resume it on the Settings page."
                )
            return
        if msg.tenant_phone in settings.staff_numbers:
            return  # internal threads aren't the bot's business

        # Tenants often send several texts in a row. Wait, and only answer the latest.
        await self._sleep(settings.debounce_seconds)
        if self.store.latest_inbound_id(msg.conversation_id) != msg.message_id:
            return
        for old in self.store.pending_drafts(msg.conversation_id):
            self.store.resolve_draft(old.id, st.SUPERSEDED, from_status=st.PENDING)

        thread = await self.rc.get_thread(msg.conversation_id, settings.thread_lookback_days)
        if not any(m.id == msg.message_id for m in thread):
            # The message store can lag the webhook by a moment.
            thread.append(ThreadMessage(msg.message_id, "Inbound", msg.text, msg.created))
        now = self._clock()
        decision: Decision = await self.agent.decide(thread, msg.tenant_phone, now)

        ctx = PolicyContext(
            tenant_phone=msg.tenant_phone,
            now=now,
            auto_sends_today=self.store.auto_sends_since(
                msg.tenant_phone, _local_midnight_utc(now, settings.timezone)
            ),
            paused=self.store.is_paused(),
        )
        result = evaluate(decision, ctx, settings)

        # Every draft starts pending; "no reply" calls still wait in the inbox in case Jeff disagrees.
        draft_id = self.store.create_draft(
            conversation_id=msg.conversation_id,
            tenant_phone=msg.tenant_phone,
            our_phone=msg.our_phone,
            inbound_message_id=msg.message_id,
            inbound_text=msg.text,
            thread=_thread_snapshot(thread),
            decision=dataclasses.asdict(decision),
            blockers=result.blockers,
        )

        if result.auto_send and self.store.claim_draft(draft_id):
            try:
                await self.rc.send_sms(msg.our_phone, msg.tenant_phone, decision.reply)
            except Exception:
                log.exception("auto-send failed for draft %s", draft_id)
                self.store.resolve_draft(draft_id, st.PENDING, from_status=st.CLAIMED)
                self.store.log_event(
                    "error", f"Auto-send to {display_phone(msg.tenant_phone)} failed; moved to inbox"
                )
            else:
                self.store.resolve_draft(
                    draft_id, st.AUTO_SENT, final_text=decision.reply.strip(), decided_by="bot"
                )
                self.store.record_send(draft_id, msg.tenant_phone, "auto", decision.reply)
                return

        if decision.action != "no_reply" or decision.reply:
            await self.alerter.needs_approval(
                self.store.count_pending(),
                urgent=decision.action == "flag",
                quiet=in_quiet_hours(now, settings),
            )

    async def handle_outbound(self, conversation_id: str) -> None:
        """Someone replied from RingCentral directly; retire any draft still waiting there."""
        for draft in self.store.pending_drafts(conversation_id):
            self.store.resolve_draft(
                draft.id, st.SUPERSEDED, decided_by="RingCentral", from_status=st.PENDING
            )

    # Inbox actions

    async def approve(self, draft_id: int, text: str | None, user: str) -> str:
        """Send a draft (text=None) or an edited version. Returns a short status message."""
        draft = self.store.get_draft(draft_id)
        if draft is None:
            return "That draft no longer exists."
        body = (text if text is not None else draft.reply).strip()
        if not body:
            return "Nothing to send."
        if not self.store.claim_draft(draft_id):
            return f"Already handled ({st.STATUS_LABELS.get(draft.status, draft.status)})."
        try:
            await self.rc.send_sms(draft.our_phone, draft.tenant_phone, body)
        except Exception:
            log.exception("approved send failed for draft %s", draft_id)
            self.store.resolve_draft(draft_id, st.PENDING, from_status=st.CLAIMED)
            self.store.log_event("error", f"Sending to {display_phone(draft.tenant_phone)} failed")
            return "Sending failed. The draft is still waiting; try again or reply from RingCentral."
        self.store.resolve_draft(draft_id, st.SENT, final_text=body, decided_by=user)
        self.store.record_send(draft_id, draft.tenant_phone, "approved", body)
        return "Sent."

    async def skip(self, draft_id: int, user: str) -> str:
        draft = self.store.get_draft(draft_id)
        if draft is None:
            return "That draft no longer exists."
        if not self.store.resolve_draft(draft_id, st.SKIPPED, decided_by=user, from_status=st.PENDING):
            return f"Already handled ({st.STATUS_LABELS.get(draft.status, draft.status)})."
        return "Skipped."

    async def _problem(self, message: str) -> None:
        self.store.log_event("error", message)
        await self.alerter.send(message)
