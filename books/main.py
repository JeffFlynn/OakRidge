"""Web service: QuickBooks connect flow, Intuit webhooks, Slack /books command, MCP endpoint.

Run locally:  uvicorn books.main:create_app --factory --reload --port 8001
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import html
import json
import logging
import secrets
from urllib.parse import parse_qs

import anthropic
from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from books.analyst import Analyst
from books.config import Settings, load_settings
from books.intuit import QBOError, QuickBooks, authorize_url, verify_webhook, webhook_realms
from books.service import Books
from books.store import Store
from books.tools import build_mcp
from textbot.slack import SlackClient, verify_signature

log = logging.getLogger("books")
TICK_SECONDS = 60


def build_books(settings: Settings) -> Books:
    store = Store(settings.db_path, settings.token_key)
    qbo = QuickBooks(
        store,
        settings.intuit_client_id,
        settings.intuit_client_secret,
        settings.redirect_uri,
        settings.intuit_environment,
    )
    slack = (
        SlackClient(settings.slack_bot_token, settings.slack_channel_id)
        if settings.slack_bot_token and settings.slack_channel_id
        else None
    )
    analyst = Analyst(anthropic.AsyncAnthropic(), settings.anthropic_model, settings.anthropic_effort)
    return Books(settings, store, qbo, slack, analyst)


def key_ok(settings: Settings, supplied: str | None) -> bool:
    return bool(settings.access_key and supplied) and hmac.compare_digest(
        settings.access_key.encode(), supplied.encode()
    )


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>{html.escape(title)}</title><body style='font-family:system-ui;max-width:40rem;"
        f"margin:2rem auto;padding:0 1rem'><h2>{html.escape(title)}</h2>{body}</body>",
        status_code=status,
    )


def create_app(settings: Settings | None = None, books: Books | None = None):
    if settings is None:
        logging.basicConfig(level=logging.INFO)
        settings = load_settings()
    books = books or build_books(settings)

    mcp = build_mcp(books)
    # Stateless JSON mode: every call stands alone, which suits a single small web service.
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True, host="0.0.0.0"
    )

    async def scheduler() -> None:
        while True:
            try:
                await books.tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp.session_manager.run():
            task = asyncio.create_task(scheduler()) if books.qbo.configured else None
            yield
            if task:
                task.cancel()

    api = FastAPI(title="QuickBooks portfolio", lifespan=lifespan)
    api.state.books = books

    @api.get("/health")
    async def health() -> dict:
        companies = books.store.companies()
        return {
            "ok": True,
            "companies": len(companies),
            "needs_reconnect": [c.name for c in companies if c.status != "active"],
        }

    @api.get("/books/connect")
    async def connect(key: str = "") -> Response:
        if not key_ok(settings, key):
            return _page("Not allowed", "<p>Missing or wrong key.</p>", 403)
        if not (books.qbo.configured and settings.token_key and settings.public_base_url):
            return _page(
                "Not set up yet",
                "<p>Set INTUIT_CLIENT_ID, INTUIT_CLIENT_SECRET, BOOKS_TOKEN_KEY and "
                "PUBLIC_BASE_URL first.</p>",
                500,
            )
        state = secrets.token_urlsafe(24)
        books.store.create_state(state)
        return RedirectResponse(authorize_url(settings.intuit_client_id, settings.redirect_uri, state))

    @api.get("/books/callback")
    async def callback(
        background: BackgroundTasks,
        code: str = "",
        state: str = "",
        realmId: str = "",  # Intuit names it this way
        error: str = "",
    ) -> Response:
        if error:
            return _page("QuickBooks said no", f"<p>{html.escape(error)}</p>", 400)
        if not (code and realmId and books.store.consume_state(state)):
            return _page("Link expired", "<p>Start again from the connect link.</p>", 400)
        try:
            name = await books.qbo.exchange_code(code, realmId)
        except QBOError as exc:
            log.warning("connect failed: %s", exc)
            return _page("Couldn't connect", f"<p>{html.escape(str(exc))}</p>", 502)
        background.add_task(books.sync, realmId)
        count = len(books.store.companies())
        return _page(
            f"Connected {name}",
            f"<p>{count} compan{'y' if count == 1 else 'ies'} connected. Its numbers are syncing now.</p>"
            "<p>To add another, open the connect link again and pick the next company in "
            "QuickBooks' company picker.</p>",
        )

    @api.post("/webhooks/intuit")
    async def intuit_webhook(request: Request) -> Response:
        body = await request.body()
        if not verify_webhook(settings.intuit_webhook_verifier, body, request.headers.get("intuit-signature", "")):
            return Response(status_code=401)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return Response(status_code=400)
        books.handle_webhook(webhook_realms(payload))
        return Response(status_code=200)

    @api.post("/slack/commands")
    async def slack_commands(request: Request, background: BackgroundTasks) -> Response:
        body = await request.body()
        ok = verify_signature(
            settings.slack_signing_secret,
            request.headers.get("X-Slack-Request-Timestamp", ""),
            body,
            request.headers.get("X-Slack-Signature", ""),
        )
        if not ok:
            return Response(status_code=401)
        form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        arg = form.get("text", "").strip().lower()
        if arg == "scorecard":
            background.add_task(books.post_scorecard)
            return PlainTextResponse("Building the scorecard; it'll post in the channel in a minute.")
        if arg == "alerts":
            alerts = books.store.open_alerts()
            names = {c.realm_id: c.name for c in books.store.companies()}
            lines = [f"• *{names.get(a.realm_id, a.realm_id)}*: {a.message}" for a in alerts]
            return PlainTextResponse("\n".join(lines) or "No open alerts.")
        companies = books.store.companies()
        lines = [
            f"• {c.name}: {c.status}, synced {c.last_synced_at:%b %d %H:%M} UTC"
            if c.last_synced_at
            else f"• {c.name}: {c.status}, never synced"
            for c in companies
        ]
        return PlainTextResponse(
            f"{len(companies)} QuickBooks companies connected.\n" + "\n".join(lines)
            + "\nCommands: /books status, /books alerts, /books scorecard"
        )

    async def app(scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] == "http" and (path == "/mcp" or path.startswith("/mcp/")):
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            auth = headers.get("authorization", "")
            supplied = auth[7:] if auth.lower().startswith("bearer ") else None
            if supplied is None:
                # For clients that can only take a URL (claude.ai custom connectors): /mcp?key=...
                supplied = (parse_qs(scope.get("query_string", b"").decode()).get("key") or [None])[0]
            if not key_ok(settings, supplied):
                await PlainTextResponse("Unauthorized", status_code=401)(scope, receive, send)
                return
            await mcp_app(scope, receive, send)
            return
        await api(scope, receive, send)

    return app
