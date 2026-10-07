"""Web server: RingCentral webhook, the web app, health check.

Run locally:  uvicorn textbot.main:create_app --factory --reload
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import anthropic
from fastapi import BackgroundTasks, FastAPI, Request, Response

from textbot import auth
from textbot.agent import ReplyAgent
from textbot.alerts import Alerter
from textbot.config import Settings, load_settings
from textbot.pipeline import Pipeline
from textbot.rentmanager import RentManagerClient
from textbot.ringcentral import RingCentralClient, outbound_conversation_id, parse_notification
from textbot.store import Store
from textbot.web import build_router, resolve_session_secret

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
    return Pipeline(settings, store, rc, agent, Alerter(settings, rc))


def bootstrap_admin(settings: Settings, store: Store) -> None:
    """Create the first admin from ADMIN_USERNAME / ADMIN_PASSWORD if nobody can log in yet."""
    if store.count_users() or not (settings.admin_username and settings.admin_password):
        return
    if len(settings.admin_password) < auth.MIN_PASSWORD_LENGTH:
        log.error("ADMIN_PASSWORD must be at least %d characters; no admin created", auth.MIN_PASSWORD_LENGTH)
        return
    store.add_user(settings.admin_username, auth.hash_password(settings.admin_password), is_admin=True)
    log.info("created admin user %s", settings.admin_username)


def create_app(settings: Settings | None = None, pipeline: Pipeline | None = None) -> FastAPI:
    if settings is None:
        logging.basicConfig(level=logging.INFO)
        settings = load_settings()
    pipeline = pipeline or build_pipeline(settings)
    bootstrap_admin(settings, pipeline.store)

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
                pipeline.store.log_event("error", "Couldn't create or renew the RingCentral webhook")
            await asyncio.sleep(SUBSCRIPTION_CHECK_SECONDS)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if settings.public_base_url and getattr(pipeline.rc, "configured", False):
            task = asyncio.create_task(keep_subscription_alive())
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Oak Ridge text bot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.pipeline = pipeline

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

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

    app.include_router(build_router(pipeline, resolve_session_secret(settings.session_secret)))
    return app
