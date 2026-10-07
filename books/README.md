# QuickBooks portfolio monitor (`books/`)

Keeps all of the park LLCs' QuickBooks Online companies in one place, so Claude can see
and compare them, watch for problems and report on them. It does three things:

1. **Syncs every connected company.** It saves each company's P&L by month (this year and
   last) and its bank and credit card balances. Intuit webhooks start a re-sync about 90
   seconds after anything changes in QuickBooks, and a full sync runs every 6 hours in
   case a webhook is missed.
2. **Watches and alerts in Slack.** After every sync it runs fixed rules and posts an alert
   only the first time something becomes true (see *Alerts* below).
   Every Monday at 8am it also posts a **weekly scorecard**: Claude's write-up of what
   needs attention, followed by the raw numbers table.
3. **Gives Claude access in chat.** An MCP endpoint lets Claude (claude.ai or Claude Code)
   query every entity at once, e.g. *"Rank the parks by NOI margin for Q3"*, *"Which park's
   water cost per dollar of rent is out of line?"* or *"Show me every Purchase over $2,000 at
   Meadow Ridge last month"*.

**It's read-only.** Intuit has no read-only permission, so the code enforces it: it only
sends GET requests (reports, `SELECT` queries, company info) and has no code that writes.
Adding writes later (e.g. categorizing transactions) would use the text bot's Slack
approval pattern, where Claude proposes a change and Jeff taps Approve.

## Layout

| Path | What it is |
|---|---|
| `intuit.py` | OAuth for many companies (token refresh and rotation, reconnect detection), read-only API calls, webhook signature check. |
| `reports.py` | Turns QuickBooks report JSON into stored rows, and any report into text for Claude. |
| `metrics.py` | Monthly summaries, trailing averages, the portfolio scorecard. NOI is income minus COGS minus operating expenses. |
| `checks.py` | Alert rules: plain code, no AI. |
| `service.py` | Sync, alert posting, the scheduler and the weekly scorecard. |
| `analyst.py` | The Claude call that writes the weekly scorecard. |
| `tools.py` | The MCP tools Claude uses in chat. |
| `main.py` | Web routes: connect flow, Intuit webhook, `/books` Slack command, `/mcp`. |
| `store.py` | SQLite: companies and their encrypted tokens, cached P&L, cash, alerts. |

## Alerts

| Rule | Fires when |
|---|---|
| `reconnect` 🔴 | Intuit rejected the company's refresh token. Reconnect it. |
| `reconnect_soon` | Authorization expires within 14 days. |
| `stale_sync` | No successful sync in 24h. |
| `negative_bank` 🔴 | A bank account's book balance is negative. |
| `low_cash` 🔴 | Total bank balance is below `ALERT_LOW_CASH` (default $0). |
| `uncategorized` | Money sits in Uncategorized Income/Expense, Ask My Accountant, or Suspense this year. |
| `noi_drop` | Last month's NOI is at least 20% and at least $1,000 below the 3 months before. |
| `revenue_drop` | Last month's income is at least 15% and at least $500 below the 3 months before. |
| `expense_spike` | An expense account was at least 2x its trailing average and at least $1,000 over it. |
| `net_loss` | Last month closed at a net loss. |

Each threshold can be changed with an environment variable (see `.env.example`). Alerts for a
month are judged on the last *complete* month.

## Claude tools (MCP)

`list_entities`, `portfolio_scorecard`, `entity_monthly`, `entity_pl_detail`,
`compare_account`, `cash_balances`, `open_alerts` answer from the synced cache.
`qbo_query` (any `SELECT`) and `qbo_report` (P&L detail, GL, balance sheet, aging, and so on)
go to QuickBooks live. `refresh_entity` re-syncs one company immediately.

## Setup

1. **Intuit app.** At developer.intuit.com, create an app with the *QuickBooks Online
   Accounting* scope. Under **Production > Keys & credentials**:
   - Add the redirect URI `https://<books-host>/books/callback`.
   - Fill in the app profile Intuit requires before it issues production keys (host domain,
     privacy policy and EULA URLs, and a short compliance questionnaire). Private apps that
     only connect your own companies still need this.

   Under **Webhooks**, set the endpoint to `https://<books-host>/webhooks/intuit`, tick the
   transaction entities (Purchase, Deposit, Transfer, JournalEntry, Bill, BillPayment,
   Invoice, Payment, SalesReceipt, Account), and copy the verifier token.
2. **Deploy.** `render.yaml` defines `oakridge-books`. Set the secrets listed in
   `books/.env.example`. Generate `BOOKS_TOKEN_KEY` with `python -m books.keygen` and
   `BOOKS_ACCESS_KEY` with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. **Connect each company.** Open `https://<books-host>/books/connect?key=<BOOKS_ACCESS_KEY>`,
   sign in to Intuit and pick a company. Repeat for each LLC; Intuit shows a company picker
   each time. Skip any you don't want watched (e.g. Rocky Creek).
4. **Slack.** In the existing Slack app, add a slash command `/books` with request URL
   `https://<books-host>/slack/commands`. It uses the same signing secret and bot token.
   Commands: `/books status`, `/books alerts`, `/books scorecard`.
5. **Connect Claude.**
   - Claude Code:
     `claude mcp add --transport http quickbooks https://<books-host>/mcp --header "Authorization: Bearer <BOOKS_ACCESS_KEY>"`
   - claude.ai: **Settings > Connectors > Add custom connector**, URL
     `https://<books-host>/mcp?key=<BOOKS_ACCESS_KEY>`. That URL works like a password:
     anyone who has it can read every company's books. To revoke access, change
     `BOOKS_ACCESS_KEY`.

## Known limits

- **Bank feed "For Review" items aren't in Intuit's API.** The uncategorized alert only
  sees transactions already posted to an Uncategorized account, and stale bank feeds can't
  be detected here. The `qbo-entity-audit` skill still covers those.
- Balances are QuickBooks book balances, not live bank balances.
- Reports use each company's own Cash/Accrual setting unless `ACCOUNTING_METHOD` is set. If
  the LLCs use different settings, set it so every entity is compared on the same basis.
- One instance only: the scheduler runs inside the web process, the same way the text
  bot's subscription renewal does.

## Tests

`pytest tests/test_books_*.py` runs offline with fake QuickBooks, Slack and Claude.
