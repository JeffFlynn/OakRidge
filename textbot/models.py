"""Shared data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

CATEGORIES = (
    "billing",
    "maintenance",
    "vendor",
    "lease",
    "internal",
    "ada",
    "tenant_drama",
    "closed",
    "other",
)
ACTIONS = ("send", "hold", "flag", "no_reply")


@dataclass(frozen=True)
class ThreadMessage:
    id: str
    direction: str  # "Inbound" or "Outbound"
    text: str
    created: datetime


@dataclass(frozen=True)
class InboundText:
    message_id: str
    conversation_id: str
    tenant_phone: str  # 10 digits
    our_phone: str  # 10 digits, the park line that received it
    text: str
    created: datetime


@dataclass
class Decision:
    category: str
    action: str
    reply: str
    reason: str
    tenant_verified: bool = False
    all_facts_verified: bool = False
    creates_new_commitment: bool = False
    sensitive_topic: bool = False
    lookups_done: list[str] = field(default_factory=list)
    needs_from_jeff: str = ""
    tenant_label: str = ""  # e.g. "Pat Doe, Oak Ridge Lot 12", when Rent Manager identified them

    @classmethod
    def hold(cls, reason: str, category: str = "other") -> "Decision":
        return cls(category=category, action="hold", reply="", reason=reason)
