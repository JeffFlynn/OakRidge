"""Passwords, signed session cookies, and login throttling (standard library only)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

SESSION_COOKIE = "textbot_session"
SESSION_SECONDS = 60 * 60 * 24 * 14  # stay signed in for two weeks
MIN_PASSWORD_LENGTH = 10

_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


def _password_fingerprint(password_hash: str) -> str:
    # Changing a password changes this, which signs out that user's other sessions.
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def make_session(secret: str, user_id: int, password_hash: str, now: float | None = None) -> str:
    expires = int((now or time.time()) + SESSION_SECONDS)
    payload = f"{user_id}.{expires}.{_password_fingerprint(password_hash)}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def read_session(secret: str, cookie: str, now: float | None = None) -> tuple[int, str] | None:
    """Returns (user_id, password fingerprint) for a valid, unexpired cookie, else None."""
    try:
        user_id, expires, fingerprint, sig = cookie.split(".")
        payload = f"{user_id}.{expires}.{fingerprint}"
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        if int(expires) < (now or time.time()):
            return None
        return int(user_id), fingerprint
    except (ValueError, AttributeError):
        return None


def session_matches(fingerprint: str, password_hash: str) -> bool:
    return hmac.compare_digest(fingerprint, _password_fingerprint(password_hash))


class LoginThrottle:
    """At most `limit` failed logins per key (username or IP) in `window` seconds."""

    def __init__(self, limit: int = 5, window: int = 15 * 60) -> None:
        self.limit = limit
        self.window = window
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _trim(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, *keys: str, now: float | None = None) -> bool:
        now = now or time.time()
        return any(len(self._trim(k, now)) >= self.limit for k in keys)

    def fail(self, *keys: str, now: float | None = None) -> None:
        now = now or time.time()
        for k in keys:
            self._trim(k, now).append(now)

    def clear(self, *keys: str) -> None:
        for k in keys:
            self._failures.pop(k, None)
