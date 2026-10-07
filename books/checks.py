"""Alert rules. Pure functions over cached numbers, so they're cheap to run after every sync.

Each finding has a stable key; the store opens an alert the first time a key fires,
keeps it open while it keeps firing, and resolves it when it stops.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from books.config import Thresholds
from books.metrics import account_history, average, last_complete_month, prior_months, summarize
from books.store import NEEDS_RECONNECT, CashAccount, Company, PLLine

CRITICAL = "critical"
WARNING = "warning"
UNCATEGORIZED_RE = re.compile(r"uncategori[sz]ed|ask my accountant|suspense", re.IGNORECASE)
EXPENSE_SECTIONS = ("COGS", "Expenses", "OtherExpenses")


@dataclass(frozen=True)
class Finding:
    key: str
    rule: str
    severity: str
    message: str


def _money(x: float) -> str:
    return f"-${abs(x):,.0f}" if x < 0 else f"${x:,.0f}"


def check_company(
    company: Company,
    lines: list[PLLine],
    cash: list[CashAccount],
    today: date,
    now: datetime,
    t: Thresholds,
) -> list[Finding]:
    r = company.realm_id
    out: list[Finding] = []

    # --- connection health ---
    if company.status == NEEDS_RECONNECT:
        out.append(
            Finding(
                f"reconnect:{r}",
                "reconnect",
                CRITICAL,
                "QuickBooks connection lost; reconnect it at /books/connect. "
                f"({company.last_error or 'no detail'})",
            )
        )
    elif (company.refresh_expires_at - now).days < 14:
        out.append(
            Finding(
                f"reconnect_soon:{r}",
                "reconnect_soon",
                WARNING,
                f"QuickBooks authorization expires {company.refresh_expires_at:%b %d}; "
                "reconnect it at /books/connect before then.",
            )
        )
    synced = company.last_synced_at
    if synced is None or (now - synced).total_seconds() > t.stale_sync_hours * 3600:
        when = f"since {synced:%b %d %H:%M} UTC" if synced else "ever"
        out.append(
            Finding(
                f"stale_sync:{r}",
                "stale_sync",
                WARNING,
                f"Numbers haven't synced {when}. Last error: {company.last_error or 'none'}",
            )
        )

    # --- cash ---
    for acct in cash:
        if acct.account_type == "Bank" and acct.balance < 0:
            out.append(
                Finding(
                    f"negative_bank:{r}:{acct.account_id}",
                    "negative_bank",
                    CRITICAL,
                    f"{acct.name} shows a negative balance of {_money(acct.balance)} in QuickBooks.",
                )
            )
    bank_total = sum(a.balance for a in cash if a.account_type == "Bank")
    if cash and bank_total < t.low_cash:
        out.append(
            Finding(
                f"low_cash:{r}",
                "low_cash",
                CRITICAL,
                f"Total bank balance is {_money(bank_total)}, under the {_money(t.low_cash)} floor.",
            )
        )

    # --- uncategorized money this year ---
    year = str(today.year)
    uncategorized: dict[str, float] = {}
    for line in lines:
        if line.month.startswith(year) and UNCATEGORIZED_RE.search(line.account):
            uncategorized[line.account] = uncategorized.get(line.account, 0.0) + line.amount
    for account, amount in sorted(uncategorized.items()):
        if abs(amount) >= t.uncategorized_min:
            out.append(
                Finding(
                    f"uncategorized:{r}:{account}",
                    "uncategorized",
                    WARNING,
                    f"{_money(amount)} sitting in '{account}' this year. Recategorize it so the "
                    "P&L is right.",
                )
            )

    # --- last complete month vs. the 3 months before it ---
    month = last_complete_month(today)
    summaries = summarize(lines)
    current = summaries.get(month)
    prior = prior_months(summaries, month)
    if current is not None and len(prior) >= 2:
        avg_noi = average([p.noi for p in prior])
        drop = avg_noi - current.noi
        if drop >= max(t.noi_drop_min, t.noi_drop_pct * abs(avg_noi)):
            out.append(
                Finding(
                    f"noi_drop:{r}:{month}",
                    "noi_drop",
                    WARNING,
                    f"{month} NOI was {_money(current.noi)} vs. a {_money(avg_noi)} trailing "
                    f"average ({_money(-drop)}).",
                )
            )
        avg_income = average([p.income for p in prior])
        income_drop = avg_income - current.income
        if income_drop >= max(t.revenue_drop_min, t.revenue_drop_pct * abs(avg_income)):
            out.append(
                Finding(
                    f"revenue_drop:{r}:{month}",
                    "revenue_drop",
                    WARNING,
                    f"{month} income was {_money(current.income)} vs. a {_money(avg_income)} "
                    f"trailing average ({_money(-income_drop)}).",
                )
            )
        if current.net_income < 0:
            out.append(
                Finding(
                    f"net_loss:{r}:{month}",
                    "net_loss",
                    WARNING,
                    f"{month} closed at a net loss of {_money(current.net_income)}.",
                )
            )

        prior_months_list = [p.month for p in prior]
        for (section, account), by_month in account_history(lines).items():
            if section not in EXPENSE_SECTIONS:
                continue
            amount = by_month.get(month, 0.0)
            avg = average([by_month.get(m, 0.0) for m in prior_months_list])
            if amount - avg >= t.expense_spike_min and amount >= t.expense_spike_ratio * avg:
                was = f"a {_money(avg)} trailing average" if avg else "nothing in prior months"
                out.append(
                    Finding(
                        f"expense_spike:{r}:{month}:{account}",
                        "expense_spike",
                        WARNING,
                        f"{month} '{account}' was {_money(amount)} vs. {was}.",
                    )
                )

    return out

