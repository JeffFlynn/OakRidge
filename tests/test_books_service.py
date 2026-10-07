import asyncio
from datetime import timedelta

from books.intuit import QBOError, ReconnectNeeded
from books.metrics import MonthSummary
from books.reports import parse_cash_accounts, parse_monthly_pl
from books.service import format_table

from tests.books_helpers import (
    ACCOUNTS_RESPONSE,
    MONTHS,
    NOW,
    REALM,
    REALM2,
    connect,
    make_books,
    pl_report,
    steady_accounts,
)


def run(coro):
    return asyncio.run(coro)


def setup(accounts=None, cash=ACCOUNTS_RESPONSE, **kw):
    books, qbo, slack, analyst = make_books(**kw)
    connect(books.store)
    qbo.pl[REALM] = pl_report(accounts or steady_accounts(), MONTHS)
    qbo.accounts[REALM] = cash
    qbo.names[REALM] = "Pine Ridge Estates LLC"
    return books, qbo, slack, analyst


def rules(books):
    return sorted(a.rule for a in books.store.open_alerts())


def test_steady_park_syncs_with_no_alerts():
    books, qbo, slack, _ = setup()
    run(books.sync(REALM))
    assert rules(books) == []
    assert slack.posts == []
    company = books.store.get_company(REALM)
    assert company.last_synced_at == NOW and company.last_error is None
    report_call = qbo.calls[0]
    assert report_call[3]["start_date"] == "2025-01-01" and report_call[3]["summarize_column_by"] == "Month"
    assert "accounting_method" not in report_call[3]


def test_bad_september_raises_alerts_once():
    sept = {m: 20000.0 for m in MONTHS} | {"2026-09": 14000.0}
    repairs = {m: 2000.0 for m in MONTHS} | {"2026-09": 9000.0}
    books, _, slack, _ = setup(steady_accounts({("Income", "Lot Rent"): sept, ("Expenses", "Repairs"): repairs}))
    run(books.sync(REALM))
    assert rules(books) == ["expense_spike", "net_loss", "noi_drop", "revenue_drop"]
    assert len(slack.posts) == 1
    assert "Pine Ridge Estates LLC" in slack.posts[0] and "'Repairs' was $9,000" in slack.posts[0]
    run(books.sync(REALM))  # still true: no repeat post
    assert len(slack.posts) == 1


def test_alert_resolves_when_fixed_and_reannounces_if_it_returns():
    uncat = {("Expenses", "Uncategorized Expense"): {"2026-09": 750.0}}
    books, qbo, slack, _ = setup(steady_accounts(uncat))
    run(books.sync(REALM))
    assert rules(books) == ["uncategorized"]
    qbo.pl[REALM] = pl_report(steady_accounts(), MONTHS)
    run(books.sync(REALM))
    assert rules(books) == []
    qbo.pl[REALM] = pl_report(steady_accounts(uncat), MONTHS)
    run(books.sync(REALM))
    assert len(slack.posts) == 2


def test_negative_bank_and_low_cash():
    cash = {"Account": [{"Id": "35", "Name": "Operating", "AccountType": "Bank", "CurrentBalance": -250}]}
    books, _, slack, _ = setup(cash=cash)
    run(books.sync(REALM))
    assert rules(books) == ["low_cash", "negative_bank"]
    assert ":red_circle:" in slack.posts[0]


def test_failed_sync_keeps_old_numbers_and_flags_stale_after_a_day():
    clock = [NOW]
    books, qbo, _, _ = setup(clock=lambda: clock[0])
    run(books.sync(REALM))
    qbo.error = QBOError("QuickBooks reports failed: 503")
    clock[0] = NOW + timedelta(hours=25)
    run(books.sync(REALM))
    company = books.store.get_company(REALM)
    assert company.last_synced_at == NOW and "503" in company.last_error
    assert books.store.pl_lines(REALM)  # cache kept
    assert rules(books) == ["stale_sync"]


def test_lost_connection_is_critical():
    books, qbo, slack, _ = setup()
    books.store.mark_needs_reconnect(REALM, "invalid_grant")
    qbo.error = ReconnectNeeded("needs reconnect")
    run(books.sync(REALM))
    assert "reconnect" in rules(books)
    assert "/books/connect" in slack.posts[0]


def test_webhook_sync_waits_for_debounce():
    clock = [NOW]
    books, qbo, _, _ = setup(clock=lambda: clock[0])
    run(books.sync(REALM))
    qbo.calls.clear()
    assert books.handle_webhook({REALM, "unknown-realm"}) == 1
    run(books.tick())
    assert qbo.calls == []  # too soon
    clock[0] = NOW + timedelta(seconds=91)
    run(books.tick())
    assert qbo.calls and books.store.get_company(REALM).dirty_since is None


def test_backstop_sync_every_few_hours():
    clock = [NOW]
    books, qbo, _, _ = setup(clock=lambda: clock[0])
    run(books.tick())  # never synced
    assert len([c for c in qbo.calls if c[0] == "report"]) == 1
    clock[0] = NOW + timedelta(hours=6)
    run(books.tick())
    assert len([c for c in qbo.calls if c[0] == "report"]) == 2


def test_resolve_entity_by_partial_name():
    books, _, _, _ = setup()
    connect(books.store, REALM2, "Meadow Ridge MHP LLC")
    assert books.resolve("meadow").realm_id == REALM2
    assert books.resolve(REALM).realm_id == REALM
    for bad in ("ridge", "nope"):
        try:
            books.resolve(bad)
        except LookupError:
            pass
        else:
            raise AssertionError(bad)


def test_scorecard_numbers():
    books, _, _, _ = setup()
    run(books.sync(REALM))
    data = books.scorecard()
    assert data["month"] == "2026-09"
    entity = data["entities"][0]
    assert entity["month"]["income"] == 20000 and entity["month"]["noi"] == 12000
    assert entity["month"]["net_income"] == 8000 and entity["month"]["noi_margin"] == 0.6
    assert entity["ytd"]["noi"] == 12000 * 5  # May through Sept in this sample
    assert entity["bank_balance"] == 62000.5 and entity["credit_card_balance"] == 1200
    assert data["portfolio"]["noi"] == 12000


def test_weekly_scorecard_posts_once_on_monday_morning():
    monday_7am = NOW.replace(day=12, hour=12)  # 7am Central
    clock = [monday_7am]
    books, _, slack, analyst = setup(clock=lambda: clock[0])
    run(books.sync(REALM))
    assert not books.scorecard_due()
    clock[0] = monday_7am + timedelta(hours=1)
    assert books.scorecard_due()
    run(books.tick())
    assert analyst.seen and "expense_movers" in analyst.seen[0]
    assert "Weekly scorecard: 2026-09" in slack.posts[-1] and "Portfolio steady" in slack.posts[-1]
    run(books.tick())
    assert sum("Weekly scorecard" in p for p in slack.posts) == 1


def test_format_table_handles_zero_income():
    card = {"entity": "Empty LLC", "month": MonthSummary("2026-09").as_dict(), "ytd": {"noi": 0}, "bank_balance": 0}
    data = {"entities": [card], "portfolio": {"income": 0, "noi": 0, "noi_margin": None, "ytd_noi": 0, "bank_balance": 0}}
    assert "Empty LLC" in format_table(data)


def test_parse_helpers_round_trip_into_store():
    books, _, _, _ = setup()
    lines = parse_monthly_pl(pl_report(steady_accounts(), MONTHS))
    books.store.replace_financials(REALM, lines, parse_cash_accounts(ACCOUNTS_RESPONSE))
    assert len(books.store.pl_lines(REALM, "2026-09", "2026-09")) == 5
