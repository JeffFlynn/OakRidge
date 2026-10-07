"""Shared fakes and sample QuickBooks payloads for the books tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from books.config import Settings
from books.service import Books
from books.store import Store

# Tuesday Oct 7 2026, 2pm Central
NOW = datetime(2026, 10, 7, 19, 0, tzinfo=timezone.utc)
REALM = "9130000000001"
REALM2 = "9130000000002"


def money_col(month: str) -> dict:
    y, m = month.split("-")
    return {
        "ColTitle": month,
        "ColType": "Money",
        "MetaData": [{"Name": "StartDate", "Value": f"{y}-{m}-01"}, {"Name": "ColKey", "Value": month}],
    }


def pl_report(accounts: dict[tuple[str, str], dict[str, float]], months: list[str]) -> dict:
    """A ProfitAndLoss report summarized by month. accounts: (group, name) -> {month: amount}.
    Expenses under 'Utilities:' are nested in a parent section to mimic sub-accounts."""

    def data_row(name: str, by_month: dict[str, float], aid: str) -> dict:
        cells = [{"value": name, "id": aid}]
        cells += [{"value": f"{by_month[m]:.2f}" if m in by_month else ""} for m in months]
        cells.append({"value": f"{sum(by_month.values()):.2f}"})
        return {"type": "Data", "ColData": cells}

    sections = []
    for i, group in enumerate(("Income", "COGS", "Expenses", "OtherIncome", "OtherExpenses")):
        rows = []
        nested = []
        for j, ((g, name), by_month) in enumerate(accounts.items()):
            if g != group:
                continue
            if name.startswith("Utilities:"):
                nested.append(data_row(name.split(":", 1)[1], by_month, f"{i}{j}"))
            else:
                rows.append(data_row(name, by_month, f"{i}{j}"))
        if nested:
            rows.append(
                {
                    "type": "Section",
                    "Header": {"ColData": [{"value": "Utilities"}]},
                    "Rows": {"Row": nested},
                    "Summary": {"ColData": [{"value": "Total Utilities"}]},
                }
            )
        sections.append(
            {
                "type": "Section",
                "group": group,
                "Header": {"ColData": [{"value": group}]},
                "Rows": {"Row": rows},
                "Summary": {"ColData": [{"value": f"Total {group}"}]},
            }
        )
    sections.insert(2, {"type": "Section", "group": "GrossProfit", "Summary": {"ColData": [{"value": "Gross Profit"}]}})
    return {
        "Header": {"ReportName": "ProfitAndLoss", "StartPeriod": "2025-01-01", "EndPeriod": "2026-10-07"},
        "Columns": {
            "Column": [{"ColTitle": "", "ColType": "Account"}]
            + [money_col(m) for m in months]
            + [{"ColTitle": "Total", "ColType": "Money", "MetaData": [{"Name": "ColKey", "Value": "total"}]}]
        },
        "Rows": {"Row": sections},
    }


MONTHS = ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]


def steady_accounts(overrides: dict | None = None) -> dict:
    """Six months of a park earning ~$20k with ~$8k of expenses; overrides replace whole accounts."""
    base = {
        ("Income", "Lot Rent"): {m: 20000.0 for m in MONTHS},
        ("Expenses", "Repairs"): {m: 2000.0 for m in MONTHS},
        ("Expenses", "Utilities:Water"): {m: 3000.0 for m in MONTHS},
        ("Expenses", "Management Fees"): {m: 3000.0 for m in MONTHS},
        ("OtherExpenses", "Interest"): {m: 4000.0 for m in MONTHS},
    }
    base.update(overrides or {})
    return base


ACCOUNTS_RESPONSE = {
    "Account": [
        {"Id": "35", "Name": "Operating", "FullyQualifiedName": "Operating", "AccountType": "Bank", "CurrentBalance": 52000.5, "Active": True},
        {"Id": "36", "Name": "Reserve", "AccountType": "Bank", "CurrentBalance": 10000, "Active": True},
        {"Id": "40", "Name": "Amex", "AccountType": "Credit Card", "CurrentBalance": 1200, "Active": True},
    ]
}


class FakeQBO:
    configured = True

    def __init__(self) -> None:
        self.pl: dict[str, dict] = {}
        self.accounts: dict[str, dict] = {}
        self.names: dict[str, str] = {}
        self.error: Exception | None = None
        self.calls: list[tuple] = []

    async def report(self, realm_id, name, params=None):
        self.calls.append(("report", realm_id, name, params))
        if self.error:
            raise self.error
        return self.pl[realm_id]

    async def query(self, realm_id, sql):
        self.calls.append(("query", realm_id, sql))
        if self.error:
            raise self.error
        return self.accounts.get(realm_id, {})

    async def company_name(self, realm_id):
        return self.names.get(realm_id, realm_id)


class FakeSlack:
    def __init__(self) -> None:
        self.posts: list[str] = []

    async def post(self, text, blocks=None):
        self.posts.append(text)
        return f"ts-{len(self.posts)}"


class FakeAnalyst:
    def __init__(self) -> None:
        self.seen: list[dict] = []

    async def write_scorecard(self, data):
        self.seen.append(data)
        return "*Headline* Portfolio steady."


def make_settings(**overrides) -> Settings:
    base = dict(db_path=":memory:", access_key="k3y", intuit_webhook_verifier="verifier")
    base.update(overrides)
    return Settings(**base)


def connect(store: Store, realm: str = REALM, name: str = "Pine Ridge Estates LLC", **kw) -> None:
    store.save_tokens(
        realm,
        name,
        kw.get("access", "acc"),
        kw.get("access_exp", NOW + timedelta(hours=1)),
        kw.get("refresh", "ref"),
        kw.get("refresh_exp", NOW + timedelta(days=100)),
    )


def make_books(clock=None, settings: Settings | None = None) -> tuple[Books, FakeQBO, FakeSlack, FakeAnalyst]:
    settings = settings or make_settings()
    store = Store(settings.db_path)
    qbo, slack, analyst = FakeQBO(), FakeSlack(), FakeAnalyst()
    now = [NOW]
    books = Books(settings, store, qbo, slack, analyst, clock=clock or (lambda: now[0]))
    return books, qbo, slack, analyst
