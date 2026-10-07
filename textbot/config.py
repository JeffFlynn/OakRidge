"""Settings, read from environment variables.

Safe defaults: shadow mode on, no categories allowed to auto-send.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from textbot.phone import normalize_phone


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _phones(name: str) -> frozenset[str]:
    return frozenset(p for p in (normalize_phone(x) for x in _list(name)) if p)


@dataclass(frozen=True)
class Settings:
    # Behavior
    shadow_mode: bool = True
    auto_send_categories: frozenset[str] = frozenset()
    test_numbers: frozenset[str] = frozenset()
    staff_numbers: frozenset[str] = frozenset()
    debounce_seconds: int = 45
    timezone: str = "America/Chicago"
    quiet_hours_start: int = 21  # no auto-sends from 9pm...
    quiet_hours_end: int = 8  # ...until 8am local time
    max_auto_sends_per_number_per_day: int = 3
    max_reply_chars: int = 480
    allowed_link_prefixes: tuple[str, ...] = (
        "https://stratexmhp.com/submit-request/aspen-ridge-capital",
    )
    thread_lookback_days: int = 30

    # Claude
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "medium"

    # Storage
    db_path: str = "textbot.sqlite3"

    # RingCentral
    rc_server: str = "https://platform.ringcentral.com"
    rc_client_id: str = ""
    rc_client_secret: str = ""
    rc_jwt: str = ""
    rc_webhook_verification_token: str = ""

    # Rent Manager
    rm_base_url: str = "https://ndrellc.api.rentmanager.com"
    rm_username: str = ""
    rm_password: str = ""
    rm_location_id: int = 1

    # Slack
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_channel_id: str = ""
    slack_approver_user_ids: frozenset[str] = field(default_factory=frozenset)

    # Deployment
    public_base_url: str = ""


def load_settings() -> Settings:
    return Settings(
        shadow_mode=_bool("SHADOW_MODE", True),
        auto_send_categories=frozenset(c.lower() for c in _list("AUTO_SEND_CATEGORIES")),
        test_numbers=_phones("TEST_NUMBERS"),
        staff_numbers=_phones("STAFF_NUMBERS"),
        debounce_seconds=_int("DEBOUNCE_SECONDS", 45),
        timezone=os.environ.get("TIMEZONE", "America/Chicago"),
        quiet_hours_start=_int("QUIET_HOURS_START", 21),
        quiet_hours_end=_int("QUIET_HOURS_END", 8),
        max_auto_sends_per_number_per_day=_int("MAX_AUTO_SENDS_PER_NUMBER_PER_DAY", 3),
        max_reply_chars=_int("MAX_REPLY_CHARS", 480),
        allowed_link_prefixes=tuple(_list("ALLOWED_LINK_PREFIXES"))
        or Settings.allowed_link_prefixes,
        thread_lookback_days=_int("THREAD_LOOKBACK_DAYS", 30),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5"),
        anthropic_effort=os.environ.get("ANTHROPIC_EFFORT", "medium"),
        db_path=os.environ.get("DB_PATH", "textbot.sqlite3"),
        rc_server=os.environ.get("RINGCENTRAL_SERVER", "https://platform.ringcentral.com"),
        rc_client_id=os.environ.get("RINGCENTRAL_CLIENT_ID", ""),
        rc_client_secret=os.environ.get("RINGCENTRAL_CLIENT_SECRET", ""),
        rc_jwt=os.environ.get("RINGCENTRAL_JWT", ""),
        rc_webhook_verification_token=os.environ.get("RINGCENTRAL_WEBHOOK_TOKEN", ""),
        rm_base_url=os.environ.get("RM_BASE_URL", "https://ndrellc.api.rentmanager.com"),
        rm_username=os.environ.get("RM_USERNAME", ""),
        rm_password=os.environ.get("RM_PASSWORD", ""),
        rm_location_id=_int("RM_LOCATION_ID", 1),
        slack_bot_token=os.environ.get("SLACK_BOT_TOKEN", ""),
        slack_signing_secret=os.environ.get("SLACK_SIGNING_SECRET", ""),
        slack_channel_id=os.environ.get("SLACK_CHANNEL_ID", ""),
        slack_approver_user_ids=frozenset(_list("SLACK_APPROVER_USER_IDS")),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
    )
