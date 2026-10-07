"""The bot's web app: login, inbox (Send / Edit / Skip), history, scorecard, settings."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from textbot import auth
from textbot.models import CATEGORIES
from textbot.phone import display_phone
from textbot.pipeline import Pipeline
from textbot.store import STATUS_LABELS, User

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
# Categories that can never auto-send, whatever the settings say (see policy.py).
NEVER_AUTO = {"ada", "tenant_drama", "internal"}
CATEGORY_LABELS = {
    "billing": "Billing / payments",
    "maintenance": "Maintenance",
    "vendor": "Vendors / referrals",
    "lease": "Lease / paperwork",
    "internal": "Staff / internal",
    "ada": "ADA / accommodation",
    "tenant_drama": "Tenant vs. tenant",
    "closed": "Already resolved",
    "other": "Other",
}
ACTION_LABELS = {
    "send": "Ready to send",
    "hold": "Needs your eyes",
    "flag": "Flagged",
    "no_reply": "No reply needed",
}


def build_router(pipeline: Pipeline, session_secret: str) -> APIRouter:
    settings = pipeline.settings
    store = pipeline.store
    tz = ZoneInfo(settings.timezone)
    secure_cookie = settings.public_base_url.startswith("https://")
    throttle = auth.LoginThrottle()
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def local_time(iso: str) -> str:
        try:
            return datetime.fromisoformat(iso).astimezone(tz).strftime("%a %-m/%-d %-I:%M %p")
        except (TypeError, ValueError):
            return iso

    templates.env.filters["local"] = local_time
    templates.env.filters["phone"] = display_phone
    templates.env.globals.update(
        category_labels=CATEGORY_LABELS,
        action_labels=ACTION_LABELS,
        status_labels=STATUS_LABELS,
    )

    router = APIRouter()

    # Helpers

    def current_user(request: Request) -> User | None:
        cookie = request.cookies.get(auth.SESSION_COOKIE, "")
        parsed = auth.read_session(session_secret, cookie) if cookie else None
        if not parsed:
            return None
        user = store.get_user(parsed[0])
        if user is None or not user.active or not auth.session_matches(parsed[1], user.password_hash):
            return None
        return user

    def same_origin(request: Request) -> bool:
        """Reject cross-site form posts (CSRF): the browser's Origin/Referer must be this site."""
        source = request.headers.get("origin") or request.headers.get("referer") or ""
        return bool(source) and urlparse(source).netloc == request.headers.get("host", "")

    def to_login() -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    def back(path: str, msg: str = "") -> RedirectResponse:
        return RedirectResponse(f"{path}?msg={quote(msg)}" if msg else path, status_code=303)

    def page(request: Request, name: str, user: User | None, **ctx) -> HTMLResponse:
        effective = pipeline.effective_settings()
        return templates.TemplateResponse(
            request,
            name,
            {
                "user": user,
                "msg": request.query_params.get("msg", ""),
                "pending_count": store.count_pending() if user else 0,
                "paused": store.is_paused(),
                "shadow_mode": effective.shadow_mode,
                **ctx,
            },
        )

    # Login

    @router.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Response:
        if current_user(request):
            return RedirectResponse("/", status_code=303)
        return page(request, "login.html", None, error="")

    @router.post("/login", response_class=HTMLResponse)
    async def login(request: Request, username: str = Form(""), password: str = Form("")) -> Response:
        if not same_origin(request):
            return Response(status_code=403)
        ip = request.client.host if request.client else "unknown"
        keys = (f"user:{username.lower().strip()}", f"ip:{ip}")
        if throttle.blocked(*keys):
            return page(request, "login.html", None, error="Too many attempts. Try again in 15 minutes.")
        user = store.get_user_by_name(username)
        if user is None or not user.active or not auth.check_password(password, user.password_hash):
            throttle.fail(*keys)
            return page(request, "login.html", None, error="Wrong username or password.")
        throttle.clear(*keys)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            auth.SESSION_COOKIE,
            auth.make_session(session_secret, user.id, user.password_hash),
            max_age=auth.SESSION_SECONDS,
            httponly=True,
            secure=secure_cookie,
            samesite="lax",
        )
        return resp

    @router.post("/logout")
    async def logout(request: Request) -> Response:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.SESSION_COOKIE)
        return resp

    # Inbox

    @router.get("/", response_class=HTMLResponse)
    async def inbox(request: Request) -> Response:
        user = current_user(request)
        if not user:
            return to_login()
        drafts = store.pending_drafts()
        # Flagged first, then everything else oldest-first.
        drafts.sort(key=lambda d: (d.action != "flag", d.created_at))
        day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        recent_error = next(
            (e for e in store.recent_events(20) if e["level"] == "error" and e["created_at"] >= day_ago),
            None,
        )
        return page(request, "inbox.html", user, drafts=drafts, recent_error=recent_error)

    @router.post("/drafts/{draft_id}/send")
    async def send_draft(
        request: Request, draft_id: int, text: str = Form(""), edited: str = Form("")
    ) -> Response:
        user = current_user(request)
        if not user:
            return to_login()
        if not same_origin(request):
            return Response(status_code=403)
        result = await pipeline.approve(draft_id, text if edited == "1" else None, user.username)
        return back("/", result)

    @router.post("/drafts/{draft_id}/skip")
    async def skip_draft(request: Request, draft_id: int) -> Response:
        user = current_user(request)
        if not user:
            return to_login()
        if not same_origin(request):
            return Response(status_code=403)
        return back("/", await pipeline.skip(draft_id, user.username))

    # History and scorecard

    @router.get("/history", response_class=HTMLResponse)
    async def history(request: Request, q: str = "") -> Response:
        user = current_user(request)
        if not user:
            return to_login()
        query = q.strip()
        digits = "".join(ch for ch in query if ch.isdigit())
        drafts = store.recent_drafts(limit=200, phone=digits if len(digits) >= 4 else query)
        return page(request, "history.html", user, drafts=drafts, q=query)

    @router.get("/scorecard", response_class=HTMLResponse)
    async def scorecard(request: Request) -> Response:
        user = current_user(request)
        if not user:
            return to_login()
        effective = pipeline.effective_settings()
        return page(
            request,
            "scorecard.html",
            user,
            rows=store.scorecard(),
            auto_categories=effective.auto_send_categories,
        )

    # Settings

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> Response:
        user = current_user(request)
        if not user:
            return to_login()
        effective = pipeline.effective_settings()
        return page(
            request,
            "settings.html",
            user,
            categories=[c for c in CATEGORIES if c not in NEVER_AUTO],
            auto_categories=effective.auto_send_categories,
            users=store.list_users() if user.is_admin else [],
            settings=effective,
            events=store.recent_events(20),
        )

    @router.post("/settings/bot")
    async def save_bot_settings(request: Request) -> Response:
        user = current_user(request)
        if not user:
            return to_login()
        if not user.is_admin or not same_origin(request):
            return Response(status_code=403)
        form = await request.form()
        paused = form.get("paused") == "1"
        shadow = form.get("shadow_mode") == "1"
        cats = sorted(
            c for c in form.getlist("auto_send_categories") if c in CATEGORIES and c not in NEVER_AUTO
        )
        store.set_setting("paused", "1" if paused else "0")
        store.set_setting("shadow_mode", "1" if shadow else "0")
        store.set_setting("auto_send_categories", ",".join(cats))
        store.log_event(
            "info",
            f"{user.username} set: {'paused' if paused else 'running'}, "
            f"{'shadow mode' if shadow else 'live'}, auto-send: {', '.join(cats) or 'none'}",
        )
        return back("/settings", "Settings saved.")

    @router.post("/settings/pause")
    async def quick_pause(request: Request) -> Response:
        """One-tap pause from the banner; any signed-in user may pause (only admins resume)."""
        user = current_user(request)
        if not user:
            return to_login()
        if not same_origin(request):
            return Response(status_code=403)
        store.set_setting("paused", "1")
        store.log_event("info", f"Paused by {user.username}")
        return back("/", "Bot paused. Nothing will auto-send.")

    @router.post("/settings/users")
    async def add_user(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
        is_admin: str = Form(""),
    ) -> Response:
        user = current_user(request)
        if not user:
            return to_login()
        if not user.is_admin or not same_origin(request):
            return Response(status_code=403)
        username = username.strip()
        if not username or len(password) < auth.MIN_PASSWORD_LENGTH:
            return back("/settings", f"Username required; password must be {auth.MIN_PASSWORD_LENGTH}+ characters.")
        if store.get_user_by_name(username):
            return back("/settings", "That username already exists.")
        store.add_user(username, auth.hash_password(password), is_admin == "1")
        store.log_event("info", f"{user.username} added user {username}")
        return back("/settings", f"Added {username}.")

    @router.post("/settings/users/{user_id}/toggle")
    async def toggle_user(request: Request, user_id: int) -> Response:
        user = current_user(request)
        if not user:
            return to_login()
        if not user.is_admin or not same_origin(request):
            return Response(status_code=403)
        target = store.get_user(user_id)
        if target is None or target.id == user.id:
            return back("/settings", "You can't disable your own account.")
        store.set_user_active(target.id, not target.active)
        store.log_event("info", f"{user.username} {'disabled' if target.active else 'enabled'} {target.username}")
        return back("/settings", f"{'Disabled' if target.active else 'Enabled'} {target.username}.")

    @router.post("/account/password")
    async def change_password(
        request: Request, current: str = Form(""), new: str = Form("")
    ) -> Response:
        user = current_user(request)
        if not user:
            return to_login()
        if not same_origin(request):
            return Response(status_code=403)
        if not auth.check_password(current, user.password_hash):
            return back("/settings", "Current password is wrong.")
        if len(new) < auth.MIN_PASSWORD_LENGTH:
            return back("/settings", f"New password must be {auth.MIN_PASSWORD_LENGTH}+ characters.")
        new_hash = auth.hash_password(new)
        store.set_password(user.id, new_hash)
        # Re-issue this browser's cookie; other sessions are signed out by the hash change.
        resp = back("/settings", "Password changed.")
        resp.set_cookie(
            auth.SESSION_COOKIE,
            auth.make_session(session_secret, user.id, new_hash),
            max_age=auth.SESSION_SECONDS,
            httponly=True,
            secure=secure_cookie,
            samesite="lax",
        )
        return resp

    return router


def resolve_session_secret(configured: str) -> str:
    if configured:
        return configured
    log.warning("SESSION_SECRET is not set; everyone will be signed out whenever the app restarts")
    return secrets.token_hex(32)
