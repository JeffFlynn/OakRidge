"""Keeps every company's numbers fresh, raises alerts, and sends the weekly scorecard."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from books import checks, metrics
from books.config import Settings
from books.intuit import QBOError, QuickBooks
from books.reports import parse_cash_accounts, parse_monthly_pl
from books.store import Company, Store

log = logging.getLogger(__name__)

CASH_QUERY = (
    "SELECT Id, Name, FullyQualifiedName, AccountType, CurrentBalance, Active FROM Account "
    "WHERE AccountType IN ('Bank', 'Credit Card')"
)
SEVERITY_ICON = {checks.CRITICAL: ":red_circle:", checks.WARNING: ":large_yellow_circle:"}


class Poster(Protocol):
    async def post(self, text: str, blocks: list[dict] | None = None) -> str: ...


class Writer(Protocol):
    async def write_scorecard(self, data: dict) -> str: ...


class Books:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        qbo: QuickBooks,
        slack: Poster | None,
        analyst: Writer | None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.settings = settings
        self.store = store
        self.qbo = qbo
        self.slack = slack
        self.analyst = analyst
        self._clock = clock

    def today(self) -> date:
        return self._clock().astimezone(ZoneInfo(self.settings.timezone)).date()

    # --- finding companies ---

    def resolve(self, entity: str) -> Company:
        """A company by realm id or by (part of) its name, case-insensitive."""
        companies = self.store.companies()
        needle = entity.strip().lower()
        exact = [c for c in companies if needle in (c.realm_id, c.name.lower())]
        if len(exact) == 1:
            return exact[0]
        matches = [c for c in companies if needle in c.name.lower()]
        if len(matches) == 1:
            return matches[0]
        names = ", ".join(c.name for c in (matches or companies))
        if not matches:
            raise LookupError(f"No connected entity matches '{entity}'. Connected: {names}")
        raise LookupError(f"'{entity}' matches several entities: {names}. Be more specific.")

    # --- syncing ---

    async def sync(self, realm_id: str) -> None:
        """Pull the P&L by month and cash balances for one company, then re-run its checks."""
        started = self._clock()
        today = self.today()
        start = date(today.year - self.settings.history_years + 1, 1, 1)
        params = {
            "start_date": start.isoformat(),
            "end_date": today.isoformat(),
            "summarize_column_by": "Month",
        }
        if self.settings.accounting_method:
            params["accounting_method"] = self.settings.accounting_method
        try:
            pl = await self.qbo.report(realm_id, "ProfitAndLoss", params)
            accounts = await self.qbo.query(realm_id, CASH_QUERY)
            name = await self.qbo.company_name(realm_id)
        except QBOError as exc:
            log.warning("sync failed for %s: %s", realm_id, exc)
            self.store.mark_synced(realm_id, started, error=str(exc)[:500])
        else:
            self.store.replace_financials(realm_id, parse_monthly_pl(pl), parse_cash_accounts(accounts))
            self.store.set_name(realm_id, name)
            self.store.mark_synced(realm_id, started)
            self.store.clear_dirty(realm_id, if_before=started)
        self.evaluate(realm_id)
        await self.notify()

    def evaluate(self, realm_id: str) -> list[checks.Finding]:
        company = self.store.get_company(realm_id)
        if company is None:
            return []
        now = self._clock()
        findings = checks.check_company(
            company,
            self.store.pl_lines(realm_id),
            self.store.cash(realm_id),
            self.today(),
            now,
            self.settings.thresholds,
        )
        for f in findings:
            self.store.upsert_alert(f.key, realm_id, f.rule, f.severity, f.message, now)
        self.store.resolve_missing_alerts(realm_id, {f.key for f in findings}, now)
        return findings

    def handle_webhook(self, realm_ids: set[str]) -> int:
        """Mark companies changed in QuickBooks; the scheduler syncs them after a short wait."""
        now = self._clock()
        return sum(self.store.mark_dirty(r, now) for r in realm_ids)

    async def tick(self) -> None:
        """One scheduler pass: debounced webhook syncs, the periodic backstop, the weekly scorecard."""
        now = self._clock()
        debounce = timedelta(seconds=self.settings.webhook_debounce_seconds)
        full = timedelta(hours=self.settings.full_sync_hours)
        for c in self.store.companies():
            due_webhook = c.dirty_since is not None and now - c.dirty_since >= debounce
            due_backstop = c.last_synced_at is None or now - c.last_synced_at >= full
            # A company that needs reconnecting can't sync; keep its alerts current.
            if c.status != "active":
                self.evaluate(c.realm_id)
                continue
            if due_webhook or due_backstop:
                try:
                    await self.sync(c.realm_id)
                except Exception:
                    log.exception("sync crashed for %s", c.realm_id)
        await self.notify()
        if self.scorecard_due():
            await self.post_scorecard()

    # --- Slack ---

    async def notify(self) -> None:
        """Post newly opened alerts to Slack, one message per company."""
        if self.slack is None:
            return
        pending = self.store.unnotified_alerts()
        if not pending:
            return
        names = {c.realm_id: c.name for c in self.store.companies()}
        by_company: dict[str, list] = defaultdict(list)
        for a in pending:
            by_company[a.realm_id].append(a)
        for realm_id, alerts in by_company.items():
            lines = [f"*{names.get(realm_id, realm_id)}*"]
            lines += [f"{SEVERITY_ICON.get(a.severity, '')} {a.message}" for a in alerts]
            text = "\n".join(lines)
            try:
                await self.slack.post(text)
            except Exception:
                log.exception("could not post alerts for %s", realm_id)
                continue
            self.store.mark_notified([a.id for a in alerts], self._clock())

    def scorecard_due(self) -> bool:
        local = self._clock().astimezone(ZoneInfo(self.settings.timezone))
        if local.weekday() != self.settings.scorecard_weekday or local.hour < self.settings.scorecard_hour:
            return False
        return self.store.get_setting("last_scorecard") != local.date().isoformat()

    def scorecard(self, month: str | None = None) -> dict:
        month = month or metrics.last_complete_month(self.today())
        cards = [
            metrics.entity_scorecard(
                c.realm_id, c.name, self.store.pl_lines(c.realm_id), self.store.cash(c.realm_id), month
            )
            for c in self.store.companies()
        ]
        return {"as_of": self.today().isoformat(), "month": month, **metrics.portfolio_scorecard(cards)}

    def expense_movers(self, month: str, limit: int = 5) -> dict[str, list[dict]]:
        """Per entity, the expense accounts that moved most vs. their trailing average."""
        out: dict[str, list[dict]] = {}
        prior = [metrics.month_add(month, -i) for i in range(1, metrics.TRAILING_MONTHS + 1)]
        for c in self.store.companies():
            rows = []
            for (section, account), by_month in metrics.account_history(self.store.pl_lines(c.realm_id)).items():
                if section not in checks.EXPENSE_SECTIONS:
                    continue
                now_amt = by_month.get(month, 0.0)
                avg = metrics.average([by_month.get(m, 0.0) for m in prior])
                rows.append(
                    {"account": account, "month": round(now_amt, 2), "trailing_avg": round(avg, 2),
                     "change": round(now_amt - avg, 2)}
                )
            rows.sort(key=lambda r: abs(r["change"]), reverse=True)
            out[c.name] = rows[:limit]
        return out

    async def post_scorecard(self) -> str:
        # Mark it sent up front so a Slack or Claude outage can't cause a retry every minute.
        self.store.set_setting("last_scorecard", self.today().isoformat())
        data = self.scorecard()
        names = {c.realm_id: c.name for c in self.store.companies()}
        data["open_alerts"] = [
            {"entity": names.get(a.realm_id, a.realm_id), "severity": a.severity, "message": a.message}
            for a in self.store.open_alerts()
        ]
        data["expense_movers"] = self.expense_movers(data["month"])
        narrative = ""
        if self.analyst is not None:
            try:
                narrative = await self.analyst.write_scorecard(data)
            except Exception:
                log.exception("Claude scorecard failed; posting numbers only")
        text = f"*Weekly scorecard: {data['month']}*\n\n{narrative}\n\n{format_table(data)}".strip()
        if self.slack is not None:
            await self.slack.post(text[:39000])
        return text


def format_table(data: dict) -> str:
    """Plain numbers under Claude's write-up, so the source figures are always visible."""
    def k(x: float) -> str:
        return f"{x / 1000:,.1f}k"

    rows = [f"{'Entity':<28}{'Income':>9}{'NOI':>9}{'Margin':>8}{'YTD NOI':>10}{'Bank':>9}"]
    for e in data["entities"]:
        m = e["month"]
        margin = f"{m['noi_margin'] * 100:.0f}%" if m["noi_margin"] is not None else "-"
        rows.append(
            f"{e['entity'][:27]:<28}{k(m['income']):>9}{k(m['noi']):>9}{margin:>8}"
            f"{k(e['ytd']['noi']):>10}{k(e['bank_balance']):>9}"
        )
    p = data["portfolio"]
    margin = f"{p['noi_margin'] * 100:.0f}%" if p["noi_margin"] is not None else "-"
    rows.append(
        f"{'TOTAL':<28}{k(p['income']):>9}{k(p['noi']):>9}{margin:>8}"
        f"{k(p['ytd_noi']):>10}{k(p['bank_balance']):>9}"
    )
    return "```\n" + "\n".join(rows) + "\n```"
