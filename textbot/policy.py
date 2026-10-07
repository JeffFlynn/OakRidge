"""The final say on whether a reply goes out without Jeff.

Claude proposes; this code decides. Every rule here is deterministic, so a
tenant can't talk the bot into sending something by what they write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from textbot.config import Settings
from textbot.models import Decision

URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)


@dataclass(frozen=True)
class PolicyContext:
    tenant_phone: str
    now: datetime  # timezone-aware
    auto_sends_today: int  # auto-sends already made to this number today (local time)
    paused: bool


@dataclass
class PolicyResult:
    auto_send: bool
    blockers: list[str] = field(default_factory=list)


def in_quiet_hours(now: datetime, settings: Settings) -> bool:
    hour = now.astimezone(ZoneInfo(settings.timezone)).hour
    start, end = settings.quiet_hours_start, settings.quiet_hours_end
    if start == end:
        return False
    if start > end:  # wraps midnight, e.g. 21 -> 8
        return hour >= start or hour < end
    return start <= hour < end


def disallowed_links(text: str, settings: Settings) -> list[str]:
    found = [m.rstrip(".,!?)") for m in URL_RE.findall(text)]
    return [u for u in found if not any(u.startswith(p) for p in settings.allowed_link_prefixes)]


def evaluate(decision: Decision, ctx: PolicyContext, settings: Settings) -> PolicyResult:
    blockers: list[str] = []
    is_test_number = ctx.tenant_phone in settings.test_numbers

    if ctx.paused:
        blockers.append("Bot is paused")
    if ctx.tenant_phone in settings.staff_numbers:
        blockers.append("Staff number: never auto-reply to internal threads")
    if settings.shadow_mode and not is_test_number:
        blockers.append("Shadow mode is on")
    if decision.action != "send":
        blockers.append(f"Claude chose '{decision.action}', not 'send'")
    if (
        not is_test_number
        and decision.category.lower() not in settings.auto_send_categories
    ):
        blockers.append(f"Category '{decision.category}' is not approved for auto-send")
    if decision.category in {"ada", "tenant_drama", "internal"}:
        blockers.append(f"Category '{decision.category}' always needs Jeff")
    if not decision.all_facts_verified:
        blockers.append("Not every fact in the reply was verified")
    if decision.creates_new_commitment:
        blockers.append("Reply makes a new commitment for the park")
    if decision.sensitive_topic:
        blockers.append("Sensitive topic (legal, eviction, hostile, safety or similar)")
    if decision.category == "billing" and not decision.tenant_verified:
        blockers.append("Billing reply but tenant identity not verified in Rent Manager")

    reply = decision.reply.strip()
    if not reply:
        blockers.append("No reply text")
    elif len(reply) > settings.max_reply_chars:
        blockers.append(f"Reply is longer than {settings.max_reply_chars} characters")
    bad_links = disallowed_links(reply, settings)
    if bad_links:
        blockers.append(f"Reply contains a link that isn't on the approved list: {bad_links[0]}")

    if in_quiet_hours(ctx.now, settings):
        blockers.append("Quiet hours")
    if ctx.auto_sends_today >= settings.max_auto_sends_per_number_per_day:
        blockers.append("Daily auto-send limit reached for this number")

    return PolicyResult(auto_send=not blockers, blockers=blockers)
