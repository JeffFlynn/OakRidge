"""Deterministic numbers: monthly summaries, trailing averages, the portfolio scorecard.

NOI here is QuickBooks' "Net Operating Income": income - cost of goods sold - expenses.
Anything booked to Other Income / Other Expenses (often interest, depreciation) sits
below the line. Net income = NOI + other income - other expenses.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

from books.store import CashAccount, PLLine

TRAILING_MONTHS = 3


def month_add(month: str, n: int) -> str:
    y, m = int(month[:4]), int(month[5:7])
    total = y * 12 + (m - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def month_of(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def last_complete_month(today: date) -> str:
    return month_add(month_of(today), -1)


@dataclass
class MonthSummary:
    month: str
    income: float = 0.0
    cogs: float = 0.0
    expenses: float = 0.0
    other_income: float = 0.0
    other_expenses: float = 0.0

    @property
    def noi(self) -> float:
        return self.income - self.cogs - self.expenses

    @property
    def net_income(self) -> float:
        return self.noi + self.other_income - self.other_expenses

    @property
    def noi_margin(self) -> float | None:
        return self.noi / self.income if self.income else None

    def as_dict(self) -> dict:
        out = {k: round(v, 2) if isinstance(v, float) else v for k, v in asdict(self).items()}
        out["noi"] = round(self.noi, 2)
        out["net_income"] = round(self.net_income, 2)
        out["noi_margin"] = round(self.noi_margin, 4) if self.noi_margin is not None else None
        return out


_FIELD = {
    "Income": "income",
    "COGS": "cogs",
    "Expenses": "expenses",
    "OtherIncome": "other_income",
    "OtherExpenses": "other_expenses",
}


def summarize(lines: list[PLLine]) -> dict[str, MonthSummary]:
    out: dict[str, MonthSummary] = {}
    for line in lines:
        s = out.setdefault(line.month, MonthSummary(line.month))
        attr = _FIELD[line.section]
        setattr(s, attr, getattr(s, attr) + line.amount)
    return out


def prior_months(summaries: dict[str, MonthSummary], month: str, n: int = TRAILING_MONTHS) -> list[MonthSummary]:
    """The n months before `month` that have any activity (gaps, e.g. before acquisition, are skipped)."""
    return [summaries[m] for m in (month_add(month, -i) for i in range(1, n + 1)) if m in summaries]


def average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def account_history(lines: list[PLLine]) -> dict[tuple[str, str], dict[str, float]]:
    """(section, account) -> {month: amount}"""
    out: dict[tuple[str, str], dict[str, float]] = {}
    for line in lines:
        months = out.setdefault((line.section, line.account), {})
        months[line.month] = months.get(line.month, 0.0) + line.amount
    return out


def entity_scorecard(
    realm_id: str, name: str, lines: list[PLLine], cash: list[CashAccount], month: str
) -> dict:
    summaries = summarize(lines)
    current = summaries.get(month, MonthSummary(month))
    prior = prior_months(summaries, month)
    ytd = MonthSummary(f"{month[:4]} YTD")
    for m, s in summaries.items():
        if m[:4] == month[:4] and m <= month:
            for attr in _FIELD.values():
                setattr(ytd, attr, getattr(ytd, attr) + getattr(s, attr))
    last_year = summaries.get(month_add(month, -12))
    bank = sum(c.balance for c in cash if c.account_type == "Bank")
    cards = sum(c.balance for c in cash if c.account_type == "Credit Card")
    return {
        "realm_id": realm_id,
        "entity": name,
        "month": current.as_dict(),
        "trailing_3mo_avg": {
            "income": round(average([p.income for p in prior]), 2),
            "noi": round(average([p.noi for p in prior]), 2),
            "months_used": len(prior),
        },
        "same_month_last_year": last_year.as_dict() if last_year else None,
        "ytd": ytd.as_dict(),
        "bank_balance": round(bank, 2),
        "credit_card_balance": round(cards, 2),
    }


def portfolio_scorecard(cards: list[dict]) -> dict:
    """Totals across entities, and entities ranked by NOI for the month."""
    ranked = sorted(cards, key=lambda c: c["month"]["noi"], reverse=True)
    total = {
        k: round(sum(c["month"][k] for c in cards), 2)
        for k in ("income", "expenses", "noi", "net_income")
    }
    total["noi_margin"] = round(total["noi"] / total["income"], 4) if total["income"] else None
    total["ytd_noi"] = round(sum(c["ytd"]["noi"] for c in cards), 2)
    total["bank_balance"] = round(sum(c["bank_balance"] for c in cards), 2)
    return {"portfolio": total, "entities": ranked}
