"""SQLite storage: seen messages, drafts and their outcomes, sends, settings."""

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
    our_phone TEXT NOT NULL,
    inbound_message_id TEXT NOT NULL,
    inbound_text TEXT NOT NULL,
    category TEXT NOT NULL,
    action TEXT NOT NULL,
    reply TEXT NOT NULL,
    reason TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    blockers_json TEXT NOT NULL,
    status TEXT NOT NULL,
    final_text TEXT,
    decided_by TEXT,
    slack_ts TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS drafts_conv ON drafts (conversation_id, status);

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
"""

# Draft statuses
PENDING = "pending"  # waiting on Jeff in Slack
AUTO_SENT = "auto_sent"
SENT = "sent"  # Jeff approved (as-is or edited)
SKIPPED = "skipped"
SUPERSEDED = "superseded"  # a newer text arrived before Jeff acted
CLAIMED = "sending"  # approval in progress; guards against double taps


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Draft:
    id: int
    conversation_id: str
    tenant_phone: str
    our_phone: str
    inbound_message_id: str
    inbound_text: str
    category: str
    action: str
    reply: str
    reason: str
    blockers: list[str]
    status: str
    final_text: str | None
    decided_by: str | None
    slack_ts: str | None


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
        decision: dict,
        blockers: list[str],
        status: str,
    ) -> int:
        now = _now()
        cur = self._exec(
            "INSERT INTO drafts (conversation_id, tenant_phone, our_phone, inbound_message_id,"
            " inbound_text, category, action, reply, reason, decision_json, blockers_json,"
            " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                conversation_id,
                tenant_phone,
                our_phone,
                inbound_message_id,
                inbound_text,
                decision.get("category", "other"),
                decision.get("action", "hold"),
                decision.get("reply", ""),
                decision.get("reason", ""),
                json.dumps(decision),
                json.dumps(blockers),
                status,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)

    def get_draft(self, draft_id: int) -> Draft | None:
        row = self._exec("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return self._to_draft(row) if row else None

    def pending_drafts(self, conversation_id: str) -> list[Draft]:
        rows = self._exec(
            "SELECT * FROM drafts WHERE conversation_id = ? AND status = ?",
            (conversation_id, PENDING),
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

    def set_slack_ts(self, draft_id: int, ts: str) -> None:
        self._exec("UPDATE drafts SET slack_ts = ? WHERE id = ?", (ts, draft_id))

    @staticmethod
    def _to_draft(row: sqlite3.Row) -> Draft:
        return Draft(
            id=row["id"],
            conversation_id=row["conversation_id"],
            tenant_phone=row["tenant_phone"],
            our_phone=row["our_phone"],
            inbound_message_id=row["inbound_message_id"],
            inbound_text=row["inbound_text"],
            category=row["category"],
            action=row["action"],
            reply=row["reply"],
            reason=row["reason"],
            blockers=json.loads(row["blockers_json"]),
            status=row["status"],
            final_text=row["final_text"],
            decided_by=row["decided_by"],
            slack_ts=row["slack_ts"],
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

    # Settings (runtime switches like pause)

    def get_setting(self, key: str, default: str = "") -> str:
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

    def stats(self) -> dict[str, int]:
        rows = self._exec("SELECT status, COUNT(*) AS n FROM drafts GROUP BY status").fetchall()
        return {r["status"]: int(r["n"]) for r in rows}
