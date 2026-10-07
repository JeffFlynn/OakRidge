"""Web server: RingCentral webhook, Slack buttons and /textbot command, health check.

Run locally:  uvicorn textbot.main:create_app --factory --reload
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from urllib.parse import parse_qs

import anthropic
from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from textbot.agent import ReplyAgent
from textbot.config import Settings, load_settings
from textbot.pipeline import Pipeline
from textbot.rentmanager import RentManagerClient
from textbot.ringcentral import RingCentralClient, outbound_conversation_id, parse_notification
from textbot.slack import SlackClient, edit_modal, verify_signature
from textbot.store import Store

log = logging.getLogger("textbot")
SUBSCRIPTION_CHECK_SECONDS = 60 * 60 * 12


def build_pipeline(settings: Settings) -> Pipeline:
    store = Store(settings.db_path)
    rc = RingCentralClient(
        settings.rc_server, settings.rc_client_id, settings.rc_client_secret, settings.rc_jwt
    )
    rm = RentManagerClient(
        settings.rm_base_url, settings.rm_username, settings.rm_password, settings.rm_location_id
    )
    agent = ReplyAgent(
        anthropic.AsyncAnthropic(),
        rm,
        settings.anthropic_model,
        settings.anthropic_effort,
        settings.timezone,
    )
    slack = SlackClient(settings.slack_bot_token, settings.slack_channel_id)
    return Pipeline(settings, store, rc, agent, slack)


def create_app(settings: Settings | None = None, pipeline: Pipeline | None = None) -> FastAPI:
    if settings is None:
        logging.basicConfig(level=logging.INFO)
        settings = load_settings()
    pipeline = pipeline or build_pipeline(settings)

    async def keep_subscription_alive() -> None:
        webhook = f"{settings.public_base_url}/webhooks/ringcentral"
        while True:
            try:
                sub_id = await pipeline.rc.ensure_subscription(
                    webhook, settings.rc_webhook_verification_token
                )
                log.info("RingCentral webhook subscription %s is active", sub_id)
            except Exception:
                log.exception("could not create or renew the RingCentral subscription")
            await asyncio.sleep(SUBSCRIPTION_CHECK_SECONDS)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if settings.public_base_url and getattr(pipeline.rc, "configured", False):
            task = asyncio.create_task(keep_subscription_alive())
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Oak Ridge text bot", lifespan=lifespan)
    app.state.pipeline = pipeline

    @app.get("/health")
    async def health() -> dict:
        return {
            "ok": True,
            "shadow_mode": settings.shadow_mode,
            "paused": pipeline.store.is_paused(),
            "auto_send_categories": sorted(settings.auto_send_categories),
        }

    @app.post("/webhooks/ringcentral")
    async def ringcentral_webhook(request: Request, background: BackgroundTasks) -> Response:
        # RingCentral checks a new webhook by sending Validation-Token and expecting it echoed.
        validation = request.headers.get("Validation-Token")
        if validation:
            return Response(status_code=200, headers={"Validation-Token": validation})
        # Every real notification carries the token we registered; anything else is rejected.
        expected = settings.rc_webhook_verification_token
        if not expected or request.headers.get("Verification-Token") != expected:
            return Response(status_code=403)
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return Response(status_code=400)
        msg = parse_notification(payload)
        if msg is not None:
            background.add_task(pipeline.handle_inbound, msg)
        elif (conversation := outbound_conversation_id(payload)) is not None:
            background.add_task(pipeline.handle_outbound, conversation)
        return Response(status_code=200)

    async def _verified_body(request: Request) -> bytes | None:
        body = await request.body()
        ok = verify_signature(
            settings.slack_signing_secret,
            request.headers.get("X-Slack-Request-Timestamp", ""),
            body,
            request.headers.get("X-Slack-Signature", ""),
        )
        return body if ok else None

    def _allowed(user_id: str) -> bool:
        return not settings.slack_approver_user_ids or user_id in settings.slack_approver_user_ids

    @app.post("/slack/interactions")
    async def slack_interactions(request: Request, background: BackgroundTasks) -> Response:
        body = await _verified_body(request)
        if body is None:
            return Response(status_code=401)
        payload = json.loads(parse_qs(body.decode())["payload"][0])
        user_id = (payload.get("user") or {}).get("id", "")
        if not _allowed(user_id):
            return Response(status_code=200)

        if payload.get("type") == "block_actions":
            action = (payload.get("actions") or [{}])[0]
            draft_id = int(action.get("value", "0"))
            if action.get("action_id") == "send":
                background.add_task(pipeline.approve, draft_id, None, user_id)
            elif action.get("action_id") == "skip":
                background.add_task(pipeline.skip, draft_id, user_id)
            elif action.get("action_id") == "edit":
                draft = pipeline.store.get_draft(draft_id)
                if draft is not None and draft.status == "pending":
                    # Slack trigger ids expire in 3 seconds, so open the modal right away.
                    await pipeline.slack.open_modal(payload["trigger_id"], edit_modal(draft))
            return Response(status_code=200)

        if payload.get("type") == "view_submission":
            view = payload.get("view") or {}
            if view.get("callback_id") == "edit_reply":
                draft_id = int(view.get("private_metadata", "0"))
                text = view["state"]["values"]["reply"]["text"]["value"] or ""
                if not text.strip():
                    return JSONResponse(
                        {"response_action": "errors", "errors": {"reply": "Reply can't be empty"}}
                    )
                background.add_task(pipeline.approve, draft_id, text, user_id)
            return Response(status_code=200)

        return Response(status_code=200)

    @app.post("/slack/commands")
    async def slack_commands(request: Request) -> Response:
        body = await _verified_body(request)
        if body is None:
            return Response(status_code=401)
        form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        if not _allowed(form.get("user_id", "")):
            return PlainTextResponse("You're not allowed to control the text bot.")
        arg = form.get("text", "").strip().lower()
        if arg == "pause":
            pipeline.store.set_setting("paused", "1")
            return PlainTextResponse("Text bot paused: nothing will auto-send. Drafts still come here.")
        if arg == "resume":
            pipeline.store.set_setting("paused", "0")
            return PlainTextResponse("Text bot resumed.")
        stats = pipeline.store.stats()
        mode = "shadow mode (nothing auto-sends)" if settings.shadow_mode else "live"
        return PlainTextResponse(
            f"Text bot is {'PAUSED' if pipeline.store.is_paused() else 'running'}, {mode}.\n"
            f"Auto-send categories: {', '.join(sorted(settings.auto_send_categories)) or 'none'}\n"
            f"Drafts so far: {stats or 'none'}\n"
            "Commands: /textbot pause, /textbot resume, /textbot status"
        )

    return app

