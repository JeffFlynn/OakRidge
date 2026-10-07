"""US phone number helpers."""

from __future__ import annotations

import re


def normalize_phone(raw: str | None) -> str:
    """Return a 10-digit US number, or "" if it can't be read as one."""
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def to_e164(raw: str) -> str:
    digits = normalize_phone(raw)
    return f"+1{digits}" if digits else ""


def display_phone(raw: str) -> str:
    d = normalize_phone(raw)
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if d else raw


def phone_variants(raw: str) -> list[str]:
    """Formats a phone number may be stored in on the Rent Manager side."""
    d = normalize_phone(raw)
    if not d:
        return []
    return [
        f"({d[:3]}) {d[3:6]}-{d[6:]}",
        f"{d[:3]}-{d[3:6]}-{d[6:]}",
        d,
        f"{d[:3]}.{d[3:6]}.{d[6:]}",
    ]
