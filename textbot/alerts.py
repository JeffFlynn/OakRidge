"""Text alerts to Jeff's cell (and anyone else in ALERT_NUMBERS) when something needs a person."""

from __future__ import annotations

import logging
import time

from textbot.config import Settings

log = logging.getLogger(__name__)


class Alerter:
    def __init__(self, settings: Settings, rc, clock=time.monotonic) -> None:
        self._settings = settings
        self._rc = rc
        self._clock = clock
        self._last_sent: float | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._settings.alert_numbers and self._settings.alert_from_number)

    async def needs_approval(self, pending_count: int, urgent: bool, quiet: bool) -> None:
        """Ping about the waiting queue, at most once per cooldown (urgent ones skip the cooldown)."""
        if not self.enabled or (quiet and not urgent):
            return
        now = self._clock()
        cooldown = self._settings.alert_cooldown_minutes * 60
        if not urgent and self._last_sent is not None and now - self._last_sent < cooldown:
            return
        self._last_sent = now
        noun = "text needs" if pending_count == 1 else "texts need"
        prefix = "FLAGGED: " if urgent else ""
        await self.send(f"{prefix}{pending_count} {noun} your approval: {self._settings.public_base_url}/")

    async def send(self, text: str) -> None:
        if not self.enabled:
            return
        for number in sorted(self._settings.alert_numbers):
            try:
                await self._rc.send_sms(self._settings.alert_from_number, number, f"Text bot: {text}")
            except Exception:
                log.exception("could not send alert to %s", number)
