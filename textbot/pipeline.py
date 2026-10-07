"""What happens to an inbound text, and to Jeff's taps in Slack."""

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
from textbot.policy import PolicyContext, evaluate
from textbot.slack import draft_blocks

log = logging.getLogger(__name__)


def _local_midnight_utc(now: datetime, tz: str) -> datetime:
    local = now.astimezone(ZoneInfo(tz))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


class Pipeline:
    def __init__(self, settings: Settings, store: st.Store, rc, agent, slack, clock=None) -> None:
        self.settings = settings
        self.store = store
        self.rc = rc
        self.agent = agent
        self.slack = slack
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = asyncio.sleep

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
            await self._notify(
                f":warning: Text bot error on a text from {display_phone(msg.tenant_phone)} "
                f"({type(exc).__name__}). Please handle it in RingCentral.\n> {msg.text}"
            )

    async def _process(self, msg: InboundText) -> None:
        if msg.tenant_phone in self.settings.staff_numbers:
            return  # internal threads aren't the bot's business

        # Tenants often send several texts in a row. Wait, and only answer the latest.
        await self._sleep(self.settings.debounce_seconds)
        if self.store.latest_inbound_id(msg.conversation_id) != msg.message_id:
            return

        for old in self.store.pending_drafts(msg.conversation_id):
            if self.store.resolve_draft(old.id, st.SUPERSEDED, from_status=st.PENDING):
                await self._update_card(old.id, "Superseded by a newer text", buttons=False)

        thread = await self.rc.get_thread(msg.conversation_id, self.settings.thread_lookback_days)
        if not any(m.id == msg.message_id for m in thread):
            # The message store can lag the webhook by a moment.
            thread.append(
                ThreadMessage(msg.message_id, "Inbound", msg.text, msg.created)
            )
        now = self._clock()
        decision: Decision = await self.agent.decide(thread, msg.tenant_phone, now)

        ctx = PolicyContext(
            tenant_phone=msg.tenant_phone,
            now=now,
            auto_sends_today=self.store.auto_sends_since(
                msg.tenant_phone, _local_midnight_utc(now, self.settings.timezone)
            ),
            paused=self.store.is_paused(),
        )
        result = evaluate(decision, ctx, self.settings)

        # Every draft starts pending; "no reply" calls still go to Jeff as a card in case he disagrees.
        draft_id = self.store.create_draft(
            conversation_id=msg.conversation_id,
            tenant_phone=msg.tenant_phone,
            our_phone=msg.our_phone,
            inbound_message_id=msg.message_id,
            inbound_text=msg.text,
            decision=dataclasses.asdict(decision),
            blockers=result.blockers,
            status=st.PENDING,
        )

        if result.auto_send and self.store.claim_draft(draft_id):
            try:
                await self.rc.send_sms(msg.our_phone, msg.tenant_phone, decision.reply)
            except Exception:
                log.exception("auto-send failed for draft %s", draft_id)
                self.store.resolve_draft(draft_id, st.PENDING, from_status=st.CLAIMED)
                await self._post_card(draft_id, "Auto-send failed, needs approval", buttons=True)
                return
            self.store.resolve_draft(draft_id, st.AUTO_SENT, final_text=decision.reply, decided_by="bot")
            self.store.record_send(draft_id, msg.tenant_phone, "auto", decision.reply)
            await self._post_card(draft_id, "Auto-sent", buttons=False)
        else:
            header = {
                "flag": "Flagged for Jeff",
                "no_reply": "No reply needed (Claude's call)",
            }.get(decision.action, "Needs approval")
            await self._post_card(draft_id, header, buttons=True)

    async def handle_outbound(self, conversation_id: str) -> None:
        """Someone replied from RingCentral directly; retire any card still waiting there."""
        for draft in self.store.pending_drafts(conversation_id):
            if self.store.resolve_draft(draft.id, st.SUPERSEDED, from_status=st.PENDING):
                await self._update_card(draft.id, "Answered directly in RingCentral", buttons=False)

    # Slack actions

    async def approve(self, draft_id: int, text: str | None, user: str) -> str:
        """Jeff tapped Send (text=None) or submitted an edit. Returns a short status message."""
        draft = self.store.get_draft(draft_id)
        if draft is None:
            return "That draft no longer exists."
        if not self.store.claim_draft(draft_id):
            return f"Already handled ({draft.status})."
        body = (text if text is not None else draft.reply).strip()
        if not body:
            self.store.resolve_draft(draft_id, st.PENDING, from_status=st.CLAIMED)
            return "Nothing to send."
        try:
            await self.rc.send_sms(draft.our_phone, draft.tenant_phone, body)
        except Exception:
            log.exception("approved send failed for draft %s", draft_id)
            self.store.resolve_draft(draft_id, st.PENDING, from_status=st.CLAIMED)
            await self._notify(
                f":warning: Sending to {display_phone(draft.tenant_phone)} failed. The draft is "
                "still pending: tap Send again or reply from RingCentral."
            )
            return "Sending failed. The draft is still pending; try again or send from RingCentral."
        self.store.resolve_draft(draft_id, st.SENT, final_text=body, decided_by=user)
        self.store.record_send(draft_id, draft.tenant_phone, "approved", body)
        edited = text is not None and body != draft.reply.strip()
        await self._update_card(
            draft_id, f"Sent by <@{user}>" + (" (edited)" if edited else ""), buttons=False
        )
        return "Sent."

    async def skip(self, draft_id: int, user: str) -> str:
        draft = self.store.get_draft(draft_id)
        if draft is None:
            return "That draft no longer exists."
        if not self.store.resolve_draft(draft_id, st.SKIPPED, decided_by=user, from_status=st.PENDING):
            return f"Already handled ({draft.status})."
        await self._update_card(draft_id, f"Skipped by <@{user}>", buttons=False)
        return "Skipped."

    # Slack helpers

    async def _notify(self, text: str) -> None:
        try:
            await self.slack.post(text)
        except Exception:
            log.exception("could not post to Slack")

    async def _post_card(self, draft_id: int, header: str, *, buttons: bool) -> None:
        draft = self.store.get_draft(draft_id)
        if draft is None:
            return
        try:
            ts = await self.slack.post(
                f"{header}: text from {display_phone(draft.tenant_phone)}",
                draft_blocks(draft, header, buttons=buttons),
            )
        except Exception:
            log.exception("could not post draft %s to Slack", draft_id)
            return
        self.store.set_slack_ts(draft_id, ts)

    async def _update_card(self, draft_id: int, header: str, *, buttons: bool) -> None:
        draft = self.store.get_draft(draft_id)
        if draft is None or not draft.slack_ts:
            return
        try:
            await self.slack.update(
                draft.slack_ts, header, draft_blocks(draft, header, buttons=buttons)
            )
        except Exception:
            log.exception("could not update Slack card for draft %s", draft_id)

