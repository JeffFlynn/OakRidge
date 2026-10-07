# Oak Ridge text bot

Answers inbound RingCentral texts for the parks in real time, using the rules from the
`ringcentral-single-text-reply` skill. It starts in **shadow mode**: Claude drafts a reply
for every text and posts it to Slack with **Send / Edit / Skip**; nothing goes to a tenant
until someone taps Send.

## How it works

1. A tenant texts a park line. RingCentral calls `POST /webhooks/ringcentral`.
2. The bot waits `DEBOUNCE_SECONDS` (default 45) so a burst of texts gets one answer.
3. It pulls the conversation from RingCentral and asks Claude what to do. Claude can look
   up the texter and their balance/transactions in Rent Manager (read-only).
4. Claude returns a decision: `send`, `hold`, `flag` or `no_reply`, with a draft and reasons.
5. `textbot/policy.py` makes the final call. A reply goes out on its own only if **all** of these hold:
   - shadow mode is off (or the number is in `TEST_NUMBERS`) and the bot isn't paused
   - the category is listed in `AUTO_SEND_CATEGORIES` (never `ada`, `tenant_drama`, `internal`)
   - Claude marked every fact verified, no new commitments, nothing sensitive
   - billing replies come from a tenant matched in Rent Manager
   - the reply is short and contains no links except approved ones
   - not quiet hours (9pm–8am Central), and under 3 auto-sends to that number today
   - the number isn't a staff number

   Everything else becomes a Slack card for Jeff.
6. In Slack, Jeff taps **Send**, **Edit** (opens an editor, then sends) or **Skip**. A newer text
   from the same person replaces an older pending card.

Kill switch: `/textbot pause` in Slack stops all auto-sends immediately (drafts still arrive).
`/textbot resume` and `/textbot status` do what they say.

## Layout

| Path | What it is |
|---|---|
| `textbot/prompts/reply_rules.md` | The rules Claude follows (adapted from the skill). Edit this to change behavior. |
| `textbot/policy.py` | Hard rules for auto-sending. Claude can't override these. |
| `textbot/agent.py` | The Claude call: tools, JSON decision schema. |
| `textbot/pipeline.py` | What happens to each text and each Slack tap. |
| `textbot/ringcentral.py`, `rentmanager.py`, `slack.py` | Connections to each system. |
| `textbot/store.py` | SQLite log of every text, draft, decision and send. |
| `scripts/try_scenarios.py` | Run sample conversations through Claude (fake Rent Manager data). |
| `scripts/rm_probe.py` | Check the Rent Manager connection with a real phone number. |

## Not wired up yet

- **Rent Manager endpoints are unverified.** `textbot/rentmanager.py` follows Rent Manager's REST
  conventions, but the exact paths and field names need checking against the ndrellc API. Run
  `python -m scripts.rm_probe <phone>` once credentials exist. Rent Manager may also require a
  partner token for API integrations; confirm with them.
- Onsite violation history, Stratex park maps and the Stratex request form have no connection, so
  those conversations are always held for Jeff.
- The "ignored thread" email from the skill isn't sent; those threads show up as Slack cards instead.
- Writing notes back to Rent Manager (e.g. logging a promise to pay) is a planned next step.

## Setup

```bash
pip install -r requirements-dev.txt
pytest                                   # offline tests, no credentials needed
ANTHROPIC_API_KEY=... python -m scripts.try_scenarios   # a few cents of Claude usage
uvicorn textbot.main:create_app --factory --reload      # local server
```

Credentials and settings are environment variables; see `.env.example`. Never commit real values.

### Accounts to create

1. **Rent Manager**: an API user with read-only permissions.
2. **RingCentral**: a developer app at developers.ringcentral.com, JWT auth, scopes *ReadMessages*,
   *SMS*, *SubscriptionWebhook*. Put the app's client ID/secret and the JWT in the environment.
3. **Slack**: a Slack app with bot scopes `chat:write`, `commands`; Interactivity request URL
   `https://<host>/slack/interactions`; a slash command `/textbot` pointing at
   `https://<host>/slack/commands`. Invite the bot to the approvals channel.
4. **Anthropic**: an API key from console.anthropic.com.

### Deploying (Render)

`render.yaml` defines an always-on web service with a small persistent disk for the SQLite log.
Create the service from the repo, fill in the secret environment variables, and set
`PUBLIC_BASE_URL` to the service URL. On startup the bot creates (and every 12 hours renews) the
RingCentral webhook subscription pointing at `PUBLIC_BASE_URL/webhooks/ringcentral`.

### Going from shadow mode to auto-send

After a week or two of shadow mode, compare what Claude drafted with what was actually sent
(`drafts` table: `reply` vs `final_text`, `status`). For categories where Claude's drafts were sent
unchanged nearly every time, add them to `AUTO_SEND_CATEGORIES` and set `SHADOW_MODE=false`.
