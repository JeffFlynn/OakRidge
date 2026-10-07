# Oak Ridge text bot

Answers inbound RingCentral texts for the parks in real time, using the rules from the
`ringcentral-single-text-reply` skill. It starts in **shadow mode**: Claude drafts a reply for
every text, and the draft waits in the bot's web inbox with **Send / Skip** (and an editable reply
box). Nothing goes to a tenant until someone taps Send.

## How it works

1. A tenant texts a park line. RingCentral calls `POST /webhooks/ringcentral`.
2. The bot waits `DEBOUNCE_SECONDS` (default 45) so a burst of texts gets one answer.
3. It pulls the conversation from RingCentral and asks Claude what to do. Claude can look up the
   texter and their balance/transactions in Rent Manager (read-only).
4. Claude returns a decision: `send`, `hold`, `flag` or `no_reply`, with a draft and reasons.
5. `textbot/policy.py` makes the final call. A reply goes out on its own only if **all** of these hold:
   - shadow mode is off (or the number is in `TEST_NUMBERS`) and the bot isn't paused
   - the category is approved for auto-send (never ADA, tenant-vs-tenant or staff threads)
   - Claude marked every fact verified, no new commitments, nothing sensitive
   - billing replies come from a tenant matched in Rent Manager
   - the reply is short and contains no links except approved ones
   - not quiet hours (9pm–8am Central), and under 3 auto-sends to that number today
   - the number isn't a staff or alert number

   Everything else waits in the inbox, and Jeff gets a text alert (at most one every 10 minutes;
   flagged texts alert right away; no alerts in quiet hours except flags).
6. A newer text from the same person replaces an older waiting draft. A reply sent directly from
   RingCentral retires the waiting draft, so nobody double-replies.

## The web app

| Page | What it's for |
|---|---|
| **Inbox** | Texts waiting for a person: the conversation, Claude's draft (editable), its reasoning, what it needs from you, and why it didn't send on its own. Flagged texts are on top. **Pause bot** button. |
| **History** | Every text and what happened to it, searchable by phone or name. Shows Claude's original draft when someone edited it. |
| **Scorecard** | Per type of text: how often Claude's draft was sent as-is, edited or skipped. Use it to decide what to let auto-send. |
| **Settings** | Admins: pause, shadow mode, which types may auto-send, add/disable staff logins, recent activity. Everyone: change password. |

Staff accounts can send, edit, skip and pause. Only admins can change settings or resume.

**Kill switches:** the **Pause bot** button in the inbox, or text `PAUSE BOT` from a number in
`ALERT_NUMBERS` to any park line.

## Layout

| Path | What it is |
|---|---|
| `textbot/prompts/reply_rules.md` | The rules Claude follows (adapted from the skill). Edit this to change behavior. |
| `textbot/policy.py` | Hard rules for auto-sending. Claude can't override these. |
| `textbot/agent.py` | The Claude call: tools and JSON decision schema. |
| `textbot/pipeline.py` | What happens to each text and each approval. |
| `textbot/web.py`, `templates/` | The web app. |
| `textbot/auth.py` | Passwords (scrypt), signed session cookies, login throttling. |
| `textbot/alerts.py` | Text alerts to Jeff's cell. |
| `textbot/ringcentral.py`, `rentmanager.py` | Connections to each system. |
| `textbot/store.py` | SQLite log of every text, draft, decision, send, user and event. |
| `scripts/try_scenarios.py` | Run sample conversations through Claude (fake Rent Manager data). |
| `scripts/rm_probe.py` | Check the Rent Manager connection with a real phone number. |

## Not wired up yet

- **Rent Manager endpoints are unverified.** `textbot/rentmanager.py` follows Rent Manager's REST
  conventions, but the exact paths and field names need checking against the ndrellc API. Run
  `python -m scripts.rm_probe <phone>` once credentials exist. Rent Manager may also require a
  partner token for API integrations; confirm with them.
- Onsite violation history, Stratex park maps and the Stratex request form have no connection, so
  those conversations always wait for a person.
- The "ignored thread" email from the skill isn't sent; those threads wait in the inbox instead.
- Writing notes back to Rent Manager (e.g. logging a promise to pay) is a planned next step.

## Setup

```bash
pip install -r requirements-dev.txt
pytest                                   # offline tests, no credentials needed
ANTHROPIC_API_KEY=... python -m scripts.try_scenarios   # a few cents of Claude usage
uvicorn textbot.main:create_app --factory --reload      # local server at http://127.0.0.1:8000
```

Credentials and settings are environment variables; see `.env.example`. Never commit real values.

### Accounts needed

1. **Rent Manager**: an API user with read-only permissions.
2. **RingCentral**: a developer app at developers.ringcentral.com, JWT auth, scopes *ReadMessages*,
   *SMS*, *SubscriptionWebhook*.
3. **Anthropic**: an API key from console.anthropic.com.

### Deploying (Render)

`render.yaml` defines an always-on web service with a small persistent disk for the SQLite database.
Create the service from the repo and fill in the environment variables marked `sync: false`
(Render generates `SESSION_SECRET` and `RINGCENTRAL_WEBHOOK_TOKEN`). Set `PUBLIC_BASE_URL` to the
service URL. On first start the bot creates the admin login from `ADMIN_USERNAME` /
`ADMIN_PASSWORD`, and creates (and every 12 hours renews) the RingCentral webhook pointing at
`PUBLIC_BASE_URL/webhooks/ringcentral`.

### Going from shadow mode to auto-send

After a week or two, open the **Scorecard**. For types of text where Claude's drafts were sent
as-is nearly every time across a good number of texts, tick them on the **Settings** page and turn
shadow mode off. The hard rules in `policy.py` still apply to every auto-send.
