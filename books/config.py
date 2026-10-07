"""Settings for the books service, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


@dataclass(frozen=True)
class Thresholds:
    """When a number is worth an alert. Dollar amounts are per entity per month."""

    noi_drop_pct: float = 0.20  # NOI this month vs. the trailing 3-month average
    noi_drop_min: float = 1000.0
    revenue_drop_pct: float = 0.15
    revenue_drop_min: float = 500.0
    expense_spike_ratio: float = 2.0  # an expense account at 2x its trailing average...
    expense_spike_min: float = 1000.0  # ...and at least this many dollars over it
    uncategorized_min: float = 1.0
    low_cash: float = 0.0  # total bank balance below this is an alert
    stale_sync_hours: int = 24


@dataclass(frozen=True)
class Settings:
    # Intuit developer app (developer.intuit.com), OAuth 2.0
    intuit_client_id: str = ""
    intuit_client_secret: str = ""
    intuit_environment: str = "production"  # or "sandbox"
    intuit_webhook_verifier: str = ""

    # One shared secret: opens /books/connect and authorizes the MCP endpoint.
    access_key: str = ""
    # Fernet key that encrypts QuickBooks tokens at rest (python -m books.keygen).
    token_key: str = ""

    public_base_url: str = ""
    db_path: str = "books.sqlite3"
    timezone: str = "America/Chicago"

    # Sync
    full_sync_hours: int = 6  # backstop when webhooks are missed
    webhook_debounce_seconds: int = 90  # a burst of edits becomes one sync
    history_years: int = 2  # P&L months kept: Jan 1 of (this year - N + 1) onward
    accounting_method: str = ""  # "Cash" or "Accrual"; blank = each company's own report setting

    # Weekly scorecard: weekday (0 = Monday) and local hour
    scorecard_weekday: int = 0
    scorecard_hour: int = 8

    # Claude
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "high"

    # Slack
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_channel_id: str = ""

    thresholds: Thresholds = Thresholds()

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url}/books/callback"


def load_settings() -> Settings:
    t = Thresholds
    return Settings(
        intuit_client_id=os.environ.get("INTUIT_CLIENT_ID", ""),
        intuit_client_secret=os.environ.get("INTUIT_CLIENT_SECRET", ""),
        intuit_environment=os.environ.get("INTUIT_ENVIRONMENT", "production"),
        intuit_webhook_verifier=os.environ.get("INTUIT_WEBHOOK_VERIFIER", ""),
        access_key=os.environ.get("BOOKS_ACCESS_KEY", ""),
        token_key=os.environ.get("BOOKS_TOKEN_KEY", ""),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
        db_path=os.environ.get("DB_PATH", "books.sqlite3"),
        timezone=os.environ.get("TIMEZONE", "America/Chicago"),
        full_sync_hours=_int("FULL_SYNC_HOURS", 6),
        webhook_debounce_seconds=_int("WEBHOOK_DEBOUNCE_SECONDS", 90),
        history_years=_int("HISTORY_YEARS", 2),
        accounting_method=os.environ.get("ACCOUNTING_METHOD", ""),
        scorecard_weekday=_int("SCORECARD_WEEKDAY", 0),
        scorecard_hour=_int("SCORECARD_HOUR", 8),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5"),
        anthropic_effort=os.environ.get("ANTHROPIC_EFFORT", "high"),
        slack_bot_token=os.environ.get("SLACK_BOT_TOKEN", ""),
        slack_signing_secret=os.environ.get("SLACK_SIGNING_SECRET", ""),
        slack_channel_id=os.environ.get("SLACK_CHANNEL_ID", ""),
        thresholds=Thresholds(
            noi_drop_pct=_float("ALERT_NOI_DROP_PCT", t.noi_drop_pct),
            noi_drop_min=_float("ALERT_NOI_DROP_MIN", t.noi_drop_min),
            revenue_drop_pct=_float("ALERT_REVENUE_DROP_PCT", t.revenue_drop_pct),
            revenue_drop_min=_float("ALERT_REVENUE_DROP_MIN", t.revenue_drop_min),
            expense_spike_ratio=_float("ALERT_EXPENSE_SPIKE_RATIO", t.expense_spike_ratio),
            expense_spike_min=_float("ALERT_EXPENSE_SPIKE_MIN", t.expense_spike_min),
            uncategorized_min=_float("ALERT_UNCATEGORIZED_MIN", t.uncategorized_min),
            low_cash=_float("ALERT_LOW_CASH", t.low_cash),
            stale_sync_hours=_int("ALERT_STALE_SYNC_HOURS", t.stale_sync_hours),
        ),
    )
