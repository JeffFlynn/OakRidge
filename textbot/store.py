"""SQLite storage: inbound texts, drafts and outcomes, sends, users, events, runtime settings."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    tenant_phone TEXT NOT NULL,
    our_phone TEXT NOT NULL,
    text TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS inbound_conv ON inbound (conversation_id, received_at);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    tenant_phone TEXT NOT NULL,
    tenant_label TEXT NOT NULL DEFAULT '',
    our_phone TEXT NOT NULL,
    inbound_message_id TEXT NOT NULL,
    inbound_text TEXT NOT NULL,
    thread_json TEXT NOT NULL DEFAULT '[]',
    category TEXT NOT NULL,
    action TEXT NOT NULL,
    reply TEXT NOT NULL,
    reason TEXT NOT NULL,
    needs_from_jeff TEXT NOT NULL DEFAULT '',
    decision_json TEXT NOT NULL,
    blockers_json TEXT NOT NULL,
    status TEXT NOT NULL,
    final_text TEXT,
    decided_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS drafts_conv ON drafts (conversation_id, status);
CREATE INDEX IF NOT EXISTS drafts_status ON drafts (status, created_at);

CREATE TABLE IF NOT EXISTS sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER,
    tenant_phone TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# Draft statuses
PENDING = "pending"  # waiting on a person in the inbox
CLAIMED = "sending"  # a send is in progress; guards against double taps
AUTO_SENT = "auto_sent"
SENT = "sent"  # approved by a person, as-is or edited
SKIPPED = "skipped"
SUPERSEDED = "superseded"  # a newer text arrived, or someone replied in RingCentral

STATUS_LABELS = {
    PENDING: "Waiting",
    CLAIMED: "Sending",
    AUTO_SENT: "Auto-sent",
    SENT: "Sent",
    SKIPPED: "Skipped",
    SUPERSEDED: "Superseded",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Draft:
    id: int
    conversation_id: str
    tenant_phone: str
    tenant_label: str
    our_phone: str
    inbound_message_id: str
    inbound_text: str
    thread: list[dict]
    category: str
    action: str
    reply: str
    reason: str
    needs_from_jeff: str
    blockers: list[str]
    status: str
    final_text: str | None
    decided_by: str | None
    created_at: str
    updated_at: str


@dataclass
class User:
    id: int
    username: str
    password_hash: str
    is_admin: bool
    active: bool


class Store:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    # Inbound messages

    def record_inbound(
        self,
        message_id: str,
        conversation_id: str,
        tenant_phone: str,
        our_phone: str,
        text: str,
        received_at: str,
    ) -> bool:
        """Store an inbound text. Returns False if we've already seen it."""
        cur = self._exec(
            "INSERT OR IGNORE INTO inbound VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, conversation_id, tenant_phone, our_phone, text, received_at),
        )
        return cur.rowcount == 1

    def latest_inbound_id(self, conversation_id: str) -> str | None:
        row = self._exec(
            "SELECT message_id FROM inbound WHERE conversation_id = ? "
            "ORDER BY received_at DESC, rowid DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return row["message_id"] if row else None

    # Drafts

    def create_draft(
        self,
        *,
        conversation_id: str,
        tenant_phone: str,
        our_phone: str,
        inbound_message_id: str,
        inbound_text: str,
        thread: list[dict],
        decision: dict,
        blockers: list[str],
    ) -> int:
        now = _now()
        cur = self._exec(
            "INSERT INTO drafts (conversation_id, tenant_phone, tenant_label, our_phone,"
            " inbound_message_id, inbound_text, thread_json, category, action, reply, reason,"
            " needs_from_jeff, decision_json, blockers_json, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                conversation_id,
                tenant_phone,
                decision.get("tenant_label", ""),
                our_phone,
                inbound_message_id,
                inbound_text,
                json.dumps(thread),
                decision.get("category", "other"),
                decision.get("action", "hold"),
                decision.get("reply", ""),
                decision.get("reason", ""),
                decision.get("needs_from_jeff", ""),
                json.dumps(decision),
                json.dumps(blockers),
                PENDING,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)

    def get_draft(self, draft_id: int) -> Draft | None:
        row = self._exec("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return self._to_draft(row) if row else None

    def pending_drafts(self, conversation_id: str | None = None) -> list[Draft]:
        if conversation_id is None:
            rows = self._exec(
                "SELECT * FROM drafts WHERE status = ? ORDER BY created_at ASC", (PENDING,)
            ).fetchall()
        else:
            rows = self._exec(
                "SELECT * FROM drafts WHERE conversation_id = ? AND status = ?",
                (conversation_id, PENDING),
            ).fetchall()
        return [self._to_draft(r) for r in rows]

    def count_pending(self) -> int:
        return int(
            self._exec("SELECT COUNT(*) AS n FROM drafts WHERE status = ?", (PENDING,)).fetchone()["n"]
        )

    def recent_drafts(self, limit: int = 100, phone: str = "") -> list[Draft]:
        if phone:
            rows = self._exec(
                "SELECT * FROM drafts WHERE tenant_phone LIKE ? OR tenant_label LIKE ?"
                " ORDER BY created_at DESC LIMIT ?",
                (f"%{phone}%", f"%{phone}%", limit),
            ).fetchall()
        else:
            rows = self._exec(
                "SELECT * FROM drafts ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._to_draft(r) for r in rows]

    def claim_draft(self, draft_id: int) -> bool:
        """Atomically move a pending draft to 'sending'. False if someone got there first."""
        cur = self._exec(
            "UPDATE drafts SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (CLAIMED, _now(), draft_id, PENDING),
        )
        return cur.rowcount == 1

    def resolve_draft(
        self,
        draft_id: int,
        status: str,
        *,
        final_text: str | None = None,
        decided_by: str | None = None,
        from_status: str | None = None,
    ) -> bool:
        sql = "UPDATE drafts SET status = ?, final_text = ?, decided_by = ?, updated_at = ? WHERE id = ?"
        params: tuple = (status, final_text, decided_by, _now(), draft_id)
        if from_status:
            sql += " AND status = ?"
            params += (from_status,)
        return self._exec(sql, params).rowcount == 1

    def scorecard(self) -> list[dict]:
        """Per category: how Claude's drafts fared once a person (or the bot) acted on them."""
        rows = self._exec(
            "SELECT category,"
            " SUM(status = 'sent' AND final_text = TRIM(reply)) AS unchanged,"
            " SUM(status = 'sent' AND final_text != TRIM(reply)) AS edited,"
            " SUM(status = 'skipped') AS skipped,"
            " SUM(status = 'auto_sent') AS auto_sent,"
            " SUM(status = 'pending') AS waiting,"
            " COUNT(*) AS total"
            " FROM drafts WHERE status != 'superseded' GROUP BY category ORDER BY total DESC"
        ).fetchall()
        out = []
        for r in rows:
            decided = (r["unchanged"] or 0) + (r["edited"] or 0) + (r["skipped"] or 0)
            out.append(
                {
                    "category": r["category"],
                    "unchanged": r["unchanged"] or 0,
                    "edited": r["edited"] or 0,
                    "skipped": r["skipped"] or 0,
                    "auto_sent": r["auto_sent"] or 0,
                    "waiting": r["waiting"] or 0,
                    "total": r["total"],
                    "decided": decided,
                    "unchanged_pct": round(100 * (r["unchanged"] or 0) / decided) if decided else None,
                }
            )
        return out

    @staticmethod
    def _to_draft(row: sqlite3.Row) -> Draft:
        return Draft(
            id=row["id"],
            conversation_id=row["conversation_id"],
            tenant_phone=row["tenant_phone"],
            tenant_label=row["tenant_label"],
            our_phone=row["our_phone"],
            inbound_message_id=row["inbound_message_id"],
            inbound_text=row["inbound_text"],
            thread=json.loads(row["thread_json"]),
            category=row["category"],
            action=row["action"],
            reply=row["reply"],
            reason=row["reason"],
            needs_from_jeff=row["needs_from_jeff"],
            blockers=json.loads(row["blockers_json"]),
            status=row["status"],
            final_text=row["final_text"],
            decided_by=row["decided_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # Sends

    def record_send(self, draft_id: int | None, tenant_phone: str, kind: str, text: str) -> None:
        self._exec(
            "INSERT INTO sends (draft_id, tenant_phone, kind, text, sent_at) VALUES (?,?,?,?,?)",
            (draft_id, tenant_phone, kind, text, _now()),
        )

    def auto_sends_since(self, tenant_phone: str, since_utc: datetime) -> int:
        row = self._exec(
            "SELECT COUNT(*) AS n FROM sends WHERE tenant_phone = ? AND kind = 'auto' AND sent_at >= ?",
            (tenant_phone, since_utc.isoformat()),
        ).fetchone()
        return int(row["n"])

    # Runtime settings (changed from the Settings page, override env defaults)

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._exec("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def is_paused(self) -> bool:
        return self.get_setting("paused", "0") == "1"

    # Users

    def add_user(self, username: str, password_hash: str, is_admin: bool) -> int:
        cur = self._exec(
            "INSERT INTO users (username, password_hash, is_admin, active, created_at) VALUES (?,?,?,1,?)",
            (username.strip(), password_hash, int(is_admin), _now()),
        )
        return int(cur.lastrowid)

    def get_user(self, user_id: int) -> User | None:
        row = self._exec("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._to_user(row) if row else None

    def get_user_by_name(self, username: str) -> User | None:
        row = self._exec("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
        return self._to_user(row) if row else None

    def list_users(self) -> list[User]:
        return [self._to_user(r) for r in self._exec("SELECT * FROM users ORDER BY username").fetchall()]

    def count_users(self) -> int:
        return int(self._exec("SELECT COUNT(*) AS n FROM users").fetchone()["n"])

    def set_user_active(self, user_id: int, active: bool) -> None:
        self._exec("UPDATE users SET active = ? WHERE id = ?", (int(active), user_id))

    def set_password(self, user_id: int, password_hash: str) -> None:
        self._exec("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))

    @staticmethod
    def _to_user(row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            username=row["username"],
            password_hash=row["password_hash"],
            is_admin=bool(row["is_admin"]),
            active=bool(row["active"]),
        )

    # Events (errors and notable actions, shown in the app)

    def log_event(self, level: str, message: str) -> None:
        self._exec(
            "INSERT INTO events (level, message, created_at) VALUES (?,?,?)",
            (level, message, _now()),
        )

    def recent_events(self, limit: int = 20) -> list[dict]:
        rows = self._exec(
            "SELECT level, message, created_at FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
