"""SQLite storage: connected companies and their tokens, cached P&L lines, cash, alerts."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    realm_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    access_token TEXT NOT NULL,
    access_expires_at TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    refresh_expires_at TEXT NOT NULL,
    connected_at TEXT NOT NULL,
    last_synced_at TEXT,
    last_error TEXT,
    dirty_since TEXT
);

CREATE TABLE IF NOT EXISTS pl_lines (
    realm_id TEXT NOT NULL,
    month TEXT NOT NULL,
    section TEXT NOT NULL,
    account_id TEXT NOT NULL,
    account TEXT NOT NULL,
    amount REAL NOT NULL,
    PRIMARY KEY (realm_id, month, section, account_id, account)
);

CREATE TABLE IF NOT EXISTS cash (
    realm_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    name TEXT NOT NULL,
    account_type TEXT NOT NULL,
    balance REAL NOT NULL,
    PRIMARY KEY (realm_id, account_id)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    realm_id TEXT NOT NULL,
    rule TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    resolved_at TEXT,
    notified_at TEXT
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

ACTIVE = "active"
NEEDS_RECONNECT = "needs_reconnect"
STATE_TTL = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


@dataclass
class Company:
    realm_id: str
    name: str
    status: str
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    connected_at: datetime
    last_synced_at: datetime | None
    last_error: str | None
    dirty_since: datetime | None


@dataclass
class PLLine:
    month: str  # YYYY-MM
    section: str  # Income, COGS, Expenses, OtherIncome, OtherExpenses
    account_id: str
    account: str
    amount: float


@dataclass
class CashAccount:
    account_id: str
    name: str
    account_type: str  # Bank or Credit Card
    balance: float


@dataclass
class Alert:
    id: int
    key: str
    realm_id: str
    rule: str
    severity: str
    message: str
    opened_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None
    notified_at: datetime | None


class Store:
    def __init__(self, path: str, token_key: str = "") -> None:
        # check_same_thread=False plus a lock: FastAPI runs sync code in a thread pool.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        # Without a key, tokens are stored as plain text (tests and local runs only).
        self._fernet = Fernet(token_key.encode()) if token_key else None
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def _seal(self, token: str) -> str:
        return self._fernet.encrypt(token.encode()).decode() if self._fernet else token

    def _open(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode() if self._fernet else token

    # --- companies ---

    def save_tokens(
        self,
        realm_id: str,
        name: str | None,
        access_token: str,
        access_expires_at: datetime,
        refresh_token: str,
        refresh_expires_at: datetime,
    ) -> None:
        """Insert or update a company's tokens; a reconnect also clears needs_reconnect."""
        now = _iso(_now())
        with self._lock:
            self._db.execute(
                """
                INSERT INTO companies (realm_id, name, status, access_token, access_expires_at,
                    refresh_token, refresh_expires_at, connected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (realm_id) DO UPDATE SET
                    name = COALESCE(?, companies.name),
                    status = excluded.status,
                    access_token = excluded.access_token,
                    access_expires_at = excluded.access_expires_at,
                    refresh_token = excluded.refresh_token,
                    refresh_expires_at = excluded.refresh_expires_at,
                    last_error = NULL
                """,
                (
                    realm_id,
                    name or realm_id,
                    ACTIVE,
                    self._seal(access_token),
                    _iso(access_expires_at),
                    self._seal(refresh_token),
                    _iso(refresh_expires_at),
                    now,
                    name,
                ),
            )
            self._db.commit()

    def _company(self, row: sqlite3.Row) -> Company:
        return Company(
            realm_id=row["realm_id"],
            name=row["name"],
            status=row["status"],
            access_token=self._open(row["access_token"]),
            access_expires_at=_dt(row["access_expires_at"]),
            refresh_token=self._open(row["refresh_token"]),
            refresh_expires_at=_dt(row["refresh_expires_at"]),
            connected_at=_dt(row["connected_at"]),
            last_synced_at=_dt(row["last_synced_at"]),
            last_error=row["last_error"],
            dirty_since=_dt(row["dirty_since"]),
        )

    def get_company(self, realm_id: str) -> Company | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM companies WHERE realm_id = ?", (realm_id,)
            ).fetchone()
        return self._company(row) if row else None

    def companies(self) -> list[Company]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM companies ORDER BY name").fetchall()
        return [self._company(r) for r in rows]

    def set_name(self, realm_id: str, name: str) -> None:
        self._update(realm_id, name=name)

    def mark_needs_reconnect(self, realm_id: str, error: str) -> None:
        self._update(realm_id, status=NEEDS_RECONNECT, last_error=error)

    def mark_synced(self, realm_id: str, at: datetime, error: str | None = None) -> None:
        if error:
            self._update(realm_id, last_error=error)
        else:
            self._update(realm_id, last_synced_at=_iso(at), last_error=None)

    def mark_dirty(self, realm_id: str, at: datetime) -> bool:
        """Flag a company for a sync. Keeps the earliest time so a stream of edits can't starve it."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE companies SET dirty_since = COALESCE(dirty_since, ?) WHERE realm_id = ?",
                (_iso(at), realm_id),
            )
            self._db.commit()
        return cur.rowcount > 0

    def clear_dirty(self, realm_id: str, if_before: datetime) -> None:
        """Clear the flag unless a webhook arrived after the sync started."""
        with self._lock:
            self._db.execute(
                "UPDATE companies SET dirty_since = NULL WHERE realm_id = ? AND dirty_since <= ?",
                (realm_id, _iso(if_before)),
            )
            self._db.commit()

    def _update(self, realm_id: str, **fields: str | None) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._db.execute(
                f"UPDATE companies SET {cols} WHERE realm_id = ?", (*fields.values(), realm_id)
            )
            self._db.commit()

    # --- cached financials ---

    def replace_financials(
        self, realm_id: str, lines: list[PLLine], cash: list[CashAccount]
    ) -> None:
        with self._lock:
            self._db.execute("DELETE FROM pl_lines WHERE realm_id = ?", (realm_id,))
            self._db.executemany(
                "INSERT OR REPLACE INTO pl_lines VALUES (?, ?, ?, ?, ?, ?)",
                [(realm_id, l.month, l.section, l.account_id, l.account, l.amount) for l in lines],
            )
            self._db.execute("DELETE FROM cash WHERE realm_id = ?", (realm_id,))
            self._db.executemany(
                "INSERT OR REPLACE INTO cash VALUES (?, ?, ?, ?, ?)",
                [(realm_id, c.account_id, c.name, c.account_type, c.balance) for c in cash],
            )
            self._db.commit()

    def pl_lines(
        self, realm_id: str, start_month: str = "0000-00", end_month: str = "9999-99"
    ) -> list[PLLine]:
        with self._lock:
            rows = self._db.execute(
                "SELECT month, section, account_id, account, amount FROM pl_lines "
                "WHERE realm_id = ? AND month BETWEEN ? AND ? ORDER BY month, section, account",
                (realm_id, start_month, end_month),
            ).fetchall()
        return [PLLine(**dict(r)) for r in rows]

    def cash(self, realm_id: str) -> list[CashAccount]:
        with self._lock:
            rows = self._db.execute(
                "SELECT account_id, name, account_type, balance FROM cash "
                "WHERE realm_id = ? ORDER BY account_type, name",
                (realm_id,),
            ).fetchall()
        return [CashAccount(**dict(r)) for r in rows]

    # --- alerts ---

    def _alert(self, row: sqlite3.Row) -> Alert:
        return Alert(
            id=row["id"],
            key=row["key"],
            realm_id=row["realm_id"],
            rule=row["rule"],
            severity=row["severity"],
            message=row["message"],
            opened_at=_dt(row["opened_at"]),
            last_seen_at=_dt(row["last_seen_at"]),
            resolved_at=_dt(row["resolved_at"]),
            notified_at=_dt(row["notified_at"]),
        )

    def upsert_alert(
        self, key: str, realm_id: str, rule: str, severity: str, message: str, at: datetime
    ) -> Alert:
        """Open an alert, or refresh one that's already open. A resolved alert that fires
        again reopens and will be announced again."""
        with self._lock:
            row = self._db.execute("SELECT * FROM alerts WHERE key = ?", (key,)).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO alerts (key, realm_id, rule, severity, message, opened_at, "
                    "last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (key, realm_id, rule, severity, message, _iso(at), _iso(at)),
                )
            elif row["resolved_at"] is not None:
                self._db.execute(
                    "UPDATE alerts SET severity = ?, message = ?, opened_at = ?, last_seen_at = ?, "
                    "resolved_at = NULL, notified_at = NULL WHERE key = ?",
                    (severity, message, _iso(at), _iso(at), key),
                )
            else:
                self._db.execute(
                    "UPDATE alerts SET severity = ?, message = ?, last_seen_at = ? WHERE key = ?",
                    (severity, message, _iso(at), key),
                )
            self._db.commit()
            row = self._db.execute("SELECT * FROM alerts WHERE key = ?", (key,)).fetchone()
        return self._alert(row)

    def resolve_missing_alerts(
        self, realm_id: str, still_firing: set[str], at: datetime, rules: set[str] | None = None
    ) -> int:
        """Resolve this company's open alerts that the latest check no longer raised.
        `rules` limits it to alerts produced by the checks that actually ran."""
        with self._lock:
            rows = self._db.execute(
                "SELECT key, rule FROM alerts WHERE realm_id = ? AND resolved_at IS NULL",
                (realm_id,),
            ).fetchall()
            gone = [
                r["key"]
                for r in rows
                if r["key"] not in still_firing and (rules is None or r["rule"] in rules)
            ]
            self._db.executemany(
                "UPDATE alerts SET resolved_at = ? WHERE key = ?", [(_iso(at), k) for k in gone]
            )
            self._db.commit()
        return len(gone)

    def open_alerts(self, realm_id: str | None = None) -> list[Alert]:
        sql = "SELECT * FROM alerts WHERE resolved_at IS NULL"
        args: tuple = ()
        if realm_id:
            sql += " AND realm_id = ?"
            args = (realm_id,)
        sql += " ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END, opened_at"
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [self._alert(r) for r in rows]

    def unnotified_alerts(self) -> list[Alert]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM alerts WHERE resolved_at IS NULL AND notified_at IS NULL "
                "ORDER BY realm_id, id"
            ).fetchall()
        return [self._alert(r) for r in rows]

    def mark_notified(self, alert_ids: list[int], at: datetime) -> None:
        with self._lock:
            self._db.executemany(
                "UPDATE alerts SET notified_at = ? WHERE id = ?",
                [(_iso(at), i) for i in alert_ids],
            )
            self._db.commit()

    # --- OAuth state (CSRF protection for the connect flow) ---

    def create_state(self, state: str) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM oauth_states WHERE created_at < ?", (_iso(_now() - STATE_TTL),)
            )
            self._db.execute(
                "INSERT INTO oauth_states (state, created_at) VALUES (?, ?)", (state, _iso(_now()))
            )
            self._db.commit()

    def consume_state(self, state: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM oauth_states WHERE state = ? AND created_at >= ?",
                (state, _iso(_now() - STATE_TTL)),
            )
            self._db.commit()
        return cur.rowcount > 0

    # --- settings ---

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._db.commit()
