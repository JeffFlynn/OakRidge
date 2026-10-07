"""MCP tools: how Claude (in chat or Claude Code) sees every QuickBooks entity at once.

Cached tools answer from the last sync (minutes old when webhooks are flowing). The two
live tools go straight to QuickBooks for anything the cache doesn't hold, such as
individual transactions. Nothing here writes to QuickBooks.
"""

from __future__ import annotations

import functools
import json

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from books import metrics
from books.intuit import REPORTS, QBOError
from books.reports import flatten_report
from books.service import Books

INSTRUCTIONS = """\
Read-only access to every QuickBooks Online company (one LLC per mobile home park) that
Jeff has connected. Start with list_entities or portfolio_scorecard. Entities can be named
by any unique part of their name. Cached numbers come from the last sync; use
qbo_query / qbo_report for transaction-level detail or anything not cached. Amounts are
in USD. NOI = income - COGS - operating expenses; other income/expenses are below NOI."""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
LIVE = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def _json(data: object) -> str:
    return json.dumps(data, default=str, indent=1)


def _explained(fn):
    """Let expected failures (unknown entity, QuickBooks errors, bad input) reach Claude
    with their message; the SDK hides the text of any other exception."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (LookupError, QBOError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def build_mcp(books: Books) -> MCPServer:
    mcp = MCPServer("quickbooks-portfolio", instructions=INSTRUCTIONS)

    def entity_name(realm_id: str) -> str:
        company = books.store.get_company(realm_id)
        return company.name if company else realm_id

    @mcp.tool(annotations=READ)
    @_explained
    async def list_entities() -> str:
        """Every connected QuickBooks company: id, name, connection status, when it last
        synced, bank balance, and how many alerts are open."""
        out = []
        for c in books.store.companies():
            cash = books.store.cash(c.realm_id)
            out.append(
                {
                    "realm_id": c.realm_id,
                    "name": c.name,
                    "status": c.status,
                    "last_synced_at": c.last_synced_at,
                    "last_error": c.last_error,
                    "bank_balance": round(sum(a.balance for a in cash if a.account_type == "Bank"), 2),
                    "open_alerts": len(books.store.open_alerts(c.realm_id)),
                }
            )
        return _json(out)

    @mcp.tool(annotations=READ)
    @_explained
    async def portfolio_scorecard(month: str = "") -> str:
        """Every entity's income, expenses, NOI, margin and net income for one month, with
        the trailing 3-month average, same month last year, year-to-date and cash, ranked
        by NOI, plus portfolio totals. month is YYYY-MM; blank means the last complete month."""
        return _json(books.scorecard(month or None))

    @mcp.tool(annotations=READ)
    @_explained
    async def entity_monthly(entity: str, start_month: str = "", end_month: str = "") -> str:
        """Month-by-month income, COGS, expenses, NOI, other income/expenses and net income
        for one entity. Months are YYYY-MM; blank start means 12 months back."""
        c = books.resolve(entity)
        end = end_month or metrics.month_of(books.today())
        start = start_month or metrics.month_add(end, -11)
        summaries = metrics.summarize(books.store.pl_lines(c.realm_id, start, end))
        return _json({"entity": c.name, "months": [s.as_dict() for _, s in sorted(summaries.items())]})

    @mcp.tool(annotations=READ)
    @_explained
    async def entity_pl_detail(entity: str, start_month: str = "", end_month: str = "") -> str:
        """Account-level P&L for one entity: every income and expense account's amount
        by month. Months are YYYY-MM; blank start means 3 months back."""
        c = books.resolve(entity)
        end = end_month or metrics.month_of(books.today())
        start = start_month or metrics.month_add(end, -2)
        table: dict[str, dict] = {}
        for line in books.store.pl_lines(c.realm_id, start, end):
            row = table.setdefault(f"{line.section}|{line.account}", {"section": line.section, "account": line.account})
            row[line.month] = round(row.get(line.month, 0.0) + line.amount, 2)
        return _json({"entity": c.name, "start": start, "end": end, "accounts": list(table.values())})

    @mcp.tool(annotations=READ)
    @_explained
    async def compare_account(account_contains: str, start_month: str = "", end_month: str = "") -> str:
        """One kind of income or expense across every entity, e.g. 'water', 'repairs',
        'lot rent'. Matches account names containing the text (case-insensitive) and
        totals them per entity for the period. Blank start means 12 months back."""
        end = end_month or metrics.last_complete_month(books.today())
        start = start_month or metrics.month_add(end, -11)
        needle = account_contains.lower()
        out = []
        for c in books.store.companies():
            matched: dict[str, float] = {}
            income = 0.0
            for line in books.store.pl_lines(c.realm_id, start, end):
                if line.section == "Income":
                    income += line.amount
                if needle in line.account.lower():
                    matched[line.account] = matched.get(line.account, 0.0) + line.amount
            total = sum(matched.values())
            out.append(
                {
                    "entity": c.name,
                    "total": round(total, 2),
                    "pct_of_income": round(total / income, 4) if income else None,
                    "accounts": {k: round(v, 2) for k, v in matched.items()},
                }
            )
        out.sort(key=lambda r: r["total"], reverse=True)
        return _json({"start": start, "end": end, "match": account_contains, "entities": out})

    @mcp.tool(annotations=READ)
    @_explained
    async def cash_balances(entity: str = "") -> str:
        """QuickBooks balances of bank and credit card accounts, for one entity or all.
        These are book balances as of the last sync, not live bank balances."""
        companies = [books.resolve(entity)] if entity else books.store.companies()
        return _json(
            [
                {"entity": c.name, "accounts": [vars(a) for a in books.store.cash(c.realm_id)]}
                for c in companies
            ]
        )

    @mcp.tool(annotations=READ)
    @_explained
    async def open_alerts(entity: str = "") -> str:
        """Problems the monitor has flagged and that are still true: lost connections,
        stale syncs, negative or low cash, uncategorized money, NOI or income drops,
        expense spikes, net-loss months."""
        realm = books.resolve(entity).realm_id if entity else None
        return _json(
            [
                {
                    "entity": entity_name(a.realm_id),
                    "severity": a.severity,
                    "rule": a.rule,
                    "message": a.message,
                    "since": a.opened_at,
                }
                for a in books.store.open_alerts(realm)
            ]
        )

    @mcp.tool(annotations=LIVE)
    @_explained
    async def qbo_query(entity: str, query: str) -> str:
        """Run a live, read-only QuickBooks query against one entity, in Intuit's SQL-like
        query language. Examples:
          SELECT * FROM Purchase WHERE TxnDate >= '2026-09-01' ORDERBY TxnDate DESC
          SELECT * FROM Bill WHERE Balance > '0'
          SELECT * FROM Vendor WHERE DisplayName LIKE '%Water%'
          SELECT * FROM Account WHERE AccountType = 'Expense'
        No joins, no aggregates; results are capped at 1000 rows unless MAXRESULTS is set."""
        c = books.resolve(entity)
        return _json({"entity": c.name, "result": await books.qbo.query(c.realm_id, query)})

    @mcp.tool(
        annotations=LIVE,
        description=(
            "Run a live QuickBooks report for one entity and get it back as tab-separated rows. "
            f"report is one of: {', '.join(sorted(REPORTS))}. params_json holds report parameters "
            'as a JSON object, e.g. {"start_date": "2026-09-01", "end_date": "2026-09-30", '
            '"accounting_method": "Cash"}; for ProfitAndLossDetail, GeneralLedger or '
            'TransactionList add "account": "<account id>" to narrow it.'
        ),
    )
    @_explained
    async def qbo_report(entity: str, report: str, params_json: str = "{}") -> str:
        c = books.resolve(entity)
        params = json.loads(params_json or "{}")
        if not isinstance(params, dict):
            raise ValueError("params_json must be a JSON object")
        data = await books.qbo.report(c.realm_id, report, {k: str(v) for k, v in params.items()})
        return f"Entity: {c.name}\n" + flatten_report(data)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
    @_explained
    async def refresh_entity(entity: str) -> str:
        """Re-sync one entity from QuickBooks right now (normally automatic within a couple
        of minutes of any change). Only refreshes the cache; nothing in QuickBooks changes."""
        c = books.resolve(entity)
        await books.sync(c.realm_id)
        fresh = books.store.get_company(c.realm_id)
        return _json({"entity": fresh.name, "last_synced_at": fresh.last_synced_at, "last_error": fresh.last_error})

    return mcp
