from books.reports import flatten_report, parse_cash_accounts, parse_monthly_pl

from tests.books_helpers import ACCOUNTS_RESPONSE, MONTHS, pl_report, steady_accounts


def test_parse_monthly_pl_reads_leaves_by_section_and_month():
    lines = parse_monthly_pl(pl_report(steady_accounts(), MONTHS))
    sept = {(l.section, l.account): l.amount for l in lines if l.month == "2026-09"}
    assert sept == {
        ("Income", "Lot Rent"): 20000.0,
        ("Expenses", "Repairs"): 2000.0,
        ("Expenses", "Water"): 3000.0,  # nested under the Utilities parent section
        ("Expenses", "Management Fees"): 3000.0,
        ("OtherExpenses", "Interest"): 4000.0,
    }
    assert {l.month for l in lines} == set(MONTHS)  # the Total column is ignored


def test_parse_monthly_pl_skips_blank_cells():
    accounts = steady_accounts({("Expenses", "Legal"): {"2026-09": 500.0}})
    lines = parse_monthly_pl(pl_report(accounts, MONTHS))
    legal = [l for l in lines if l.account == "Legal"]
    assert [(l.month, l.amount) for l in legal] == [("2026-09", 500.0)]


def test_parse_cash_accounts_keeps_bank_and_cards():
    accounts = parse_cash_accounts(ACCOUNTS_RESPONSE)
    assert [(a.name, a.account_type, a.balance) for a in accounts] == [
        ("Operating", "Bank", 52000.5),
        ("Reserve", "Bank", 10000.0),
        ("Amex", "Credit Card", 1200.0),
    ]


def test_flatten_report_shows_nesting_and_totals():
    text = flatten_report(pl_report(steady_accounts(), MONTHS))
    assert "ProfitAndLoss" in text.splitlines()[0]
    assert "\n  Lot Rent\t20000.00" in text
    assert "    Water\t3000.00" in text  # two levels deep
    assert "Total Utilities" in text
