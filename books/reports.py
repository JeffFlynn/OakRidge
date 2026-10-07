"""Turn QuickBooks report JSON into rows we can store and reason about."""

from __future__ import annotations

from books.store import CashAccount, PLLine

# Top-level P&L groups that hold accounts. The others (GrossProfit, NetIncome...) are totals.
PL_SECTIONS = ("Income", "COGS", "Expenses", "OtherIncome", "OtherExpenses")
CASH_TYPES = ("Bank", "Credit Card")


def _amount(raw: str | None) -> float:
    if not raw:
        return 0.0
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return 0.0


def _month_columns(report: dict) -> list[tuple[int, str]]:
    """(column index, YYYY-MM) for each monthly column; skips the label and Total columns."""
    out = []
    for i, col in enumerate((report.get("Columns") or {}).get("Column") or []):
        meta = {m.get("Name"): m.get("Value") for m in col.get("MetaData") or []}
        start = meta.get("StartDate")
        if col.get("ColType") == "Money" and start and meta.get("ColKey") != "total":
            out.append((i, start[:7]))
    return out


def parse_monthly_pl(report: dict) -> list[PLLine]:
    """Leaf account amounts per month from a ProfitAndLoss report run with
    summarize_column_by=Month. Parent accounts appear as sections whose own postings
    show up as a data row, so summing the leaves never double counts."""
    columns = _month_columns(report)
    totals: dict[tuple[str, str, str, str], float] = {}

    def walk(rows: list[dict], section: str | None) -> None:
        for row in rows:
            group = row.get("group")
            current = group if group in PL_SECTIONS else section
            if "ColData" in row:  # a data row
                if current is None:
                    continue
                cells = row["ColData"]
                account, account_id = cells[0].get("value", ""), cells[0].get("id", "")
                for idx, month in columns:
                    if idx < len(cells):
                        amount = _amount(cells[idx].get("value"))
                        if amount:
                            key = (month, current, account_id, account)
                            totals[key] = totals.get(key, 0.0) + amount
            child = (row.get("Rows") or {}).get("Row")
            if child:
                walk(child, current)

    walk((report.get("Rows") or {}).get("Row") or [], None)
    return [
        PLLine(month=m, section=s, account_id=aid, account=a, amount=round(v, 2))
        for (m, s, aid, a), v in sorted(totals.items())
    ]


def parse_cash_accounts(query_response: dict) -> list[CashAccount]:
    return [
        CashAccount(
            account_id=str(a.get("Id", "")),
            name=a.get("FullyQualifiedName") or a.get("Name", ""),
            account_type=a.get("AccountType", ""),
            balance=float(a.get("CurrentBalance") or 0.0),
        )
        for a in query_response.get("Account") or []
        if a.get("AccountType") in CASH_TYPES and a.get("Active", True)
    ]


def flatten_report(report: dict, max_rows: int = 2000) -> str:
    """Any QuickBooks report as tab-separated text, with indentation showing nesting."""
    header = report.get("Header") or {}
    cols = [c.get("ColTitle", "") for c in (report.get("Columns") or {}).get("Column") or []]
    title = (
        f"{header.get('ReportName', 'Report')}  {header.get('StartPeriod', '')} to "
        f"{header.get('EndPeriod', '')}  ({header.get('ReportBasis', '')} basis)"
    )
    lines = [title, "\t".join(cols)]

    def cells(data: list[dict], depth: int) -> str:
        values = [c.get("value", "") for c in data]
        if values:
            values[0] = "  " * depth + values[0]
        return "\t".join(values)

    def walk(rows: list[dict], depth: int) -> None:
        for row in rows:
            if len(lines) >= max_rows:
                return
            if "ColData" in row:
                lines.append(cells(row["ColData"], depth))
            if row.get("Header"):
                lines.append(cells(row["Header"].get("ColData") or [], depth))
            child = (row.get("Rows") or {}).get("Row")
            if child:
                walk(child, depth + 1)
            if row.get("Summary"):
                lines.append(cells(row["Summary"].get("ColData") or [], depth))

    walk((report.get("Rows") or {}).get("Row") or [], 0)
    if len(lines) >= max_rows:
        lines.append(f"... truncated at {max_rows} rows; narrow the date range or filters")
    return "\n".join(lines)
