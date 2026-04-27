"""sqlite-backed persistence layer. WAL mode. Stdlib only.

Schema lives here as one CREATE TABLE block per concern. No ORM.
Migrations are hand-written ALTER TABLE in MIGRATIONS.md.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tokens INTEGER NOT NULL DEFAULT 0,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, ts DESC);

CREATE TABLE IF NOT EXISTS debits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    model TEXT NOT NULL,
    usd REAL NOT NULL,
    day_utc TEXT NOT NULL,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_debits_day ON debits(day_utc);
CREATE INDEX IF NOT EXISTS idx_debits_user_day ON debits(chat_id, day_utc);

CREATE TABLE IF NOT EXISTS cooldowns (
    model TEXT PRIMARY KEY,
    until_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL,
    chat_id INTEGER,
    created_ts INTEGER NOT NULL,
    last_used_ts INTEGER,
    applied_by TEXT NOT NULL CHECK (applied_by IN ('user', 'agent'))
);
CREATE INDEX IF NOT EXISTS idx_memories_type_chat ON memories(type, chat_id);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, content='memories', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TABLE IF NOT EXISTS learn_cursor (
    daemon_name TEXT PRIMARY KEY,
    last_message_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS traces (
    run_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    started_ts INTEGER NOT NULL,
    stopped TEXT NOT NULL,
    spend_usd REAL NOT NULL DEFAULT 0,
    turns INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
"""


class BudgetExceeded(Exception):
    """Raised when global or per-user daily cap is exceeded."""

    def __init__(self, scope: str, day_total: float, cap: float):
        super().__init__(
            f"{scope} cap reached (${day_total:.4f}/${cap:.4f})"
        )
        self.scope = scope
        self.day_total = day_total
        self.cap = cap


class Persistence:
    def __init__(self, db_path: str):
        self._path = os.path.expanduser(db_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def init(self) -> None:
        """Open connection, set WAL, run schema."""
        self._conn = sqlite3.connect(self._path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ----- Messages (sliding window) -----

    def append_message(
        self, chat_id: int, role: str, content: str, *, tokens: int = 0, ts: int | None = None
    ) -> None:
        ts = ts if ts is not None else int(time.time())
        assert self._conn is not None
        self._conn.execute(
            "INSERT INTO messages(chat_id, role, content, tokens, ts) VALUES(?,?,?,?,?)",
            (chat_id, role, content, tokens, ts),
        )

    def load_history(
        self, chat_id: int, *, max_msgs: int = 20, max_tokens: int = 8000
    ) -> list[dict[str, Any]]:
        """Return recent messages for chat_id, capped by max_msgs AND max_tokens."""
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT role, content, tokens, ts FROM messages "
            "WHERE chat_id=? ORDER BY ts DESC LIMIT ?",
            (chat_id, max_msgs),
        ).fetchall()
        # Apply token cap: walk newest-first, accumulate tokens, drop overflow
        kept: list[sqlite3.Row] = []
        used = 0
        for r in rows:
            if used + r["tokens"] > max_tokens:
                break
            kept.append(r)
            used += r["tokens"]
        # Return oldest-first (chronological) for chat completion
        return [
            {"role": r["role"], "content": r["content"], "tokens": r["tokens"], "ts": r["ts"]}
            for r in reversed(kept)
        ]

    # ----- Budget debits -----

    def debit(
        self,
        *,
        chat_id: int,
        model: str,
        usd: float,
        day_utc: str,
        per_user_cap: float,
        global_cap: float,
        ts: int | None = None,
    ) -> tuple[float, float]:
        """Atomic spend debit with cap check. Raises BudgetExceeded on breach.

        Returns (new_user_total, new_global_total) after debit.
        Order: check global first (kills all users), then per-user.
        """
        ts = ts if ts is not None else int(time.time())
        assert self._conn is not None
        with self._tx():
            global_total = (self._conn.execute(
                "SELECT COALESCE(SUM(usd),0) FROM debits WHERE day_utc=?",
                (day_utc,),
            ).fetchone()[0]) + usd
            if global_total > global_cap:
                raise BudgetExceeded("global", global_total, global_cap)

            user_total = (self._conn.execute(
                "SELECT COALESCE(SUM(usd),0) FROM debits WHERE chat_id=? AND day_utc=?",
                (chat_id, day_utc),
            ).fetchone()[0]) + usd
            if user_total > per_user_cap:
                raise BudgetExceeded("per_user", user_total, per_user_cap)

            self._conn.execute(
                "INSERT INTO debits(chat_id, model, usd, day_utc, ts) VALUES(?,?,?,?,?)",
                (chat_id, model, usd, day_utc, ts),
            )
        return user_total, global_total

    def spend_today(self, day_utc: str, chat_id: int | None = None) -> float:
        assert self._conn is not None
        if chat_id is None:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(usd),0) FROM debits WHERE day_utc=?", (day_utc,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(usd),0) FROM debits WHERE chat_id=? AND day_utc=?",
                (chat_id, day_utc),
            ).fetchone()
        return float(row[0])

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """Immediate transaction for atomic spend check + insert."""
        assert self._conn is not None
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ----- Cooldowns -----

    def set_cooldown(self, model: str, *, until_ts: int) -> None:
        assert self._conn is not None
        self._conn.execute(
            "INSERT OR REPLACE INTO cooldowns(model, until_ts) VALUES(?,?)",
            (model, until_ts),
        )

    def cooled_models(self, *, now_ts: int) -> set[str]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT model FROM cooldowns WHERE until_ts > ?", (now_ts,)
        ).fetchall()
        return {row["model"] for row in rows}

    # ----- Traces -----

    def log_trace(
        self,
        *,
        run_id: str,
        chat_id: int,
        started_ts: int,
        stopped: str,
        spend_usd: float = 0.0,
        turns: int = 0,
        error: str | None = None,
    ) -> None:
        assert self._conn is not None
        self._conn.execute(
            "INSERT OR REPLACE INTO traces"
            "(run_id, chat_id, started_ts, stopped, spend_usd, turns, error)"
            " VALUES(?,?,?,?,?,?,?)",
            (run_id, chat_id, started_ts, stopped, spend_usd, turns, error),
        )

    # ----- Memories -----

    def memory_save(
        self,
        *,
        type: str,
        content: str,
        source: str,
        chat_id: int | None,
        created_ts: int,
        applied_by: str,
        last_used_ts: int | None = None,
    ) -> int:
        assert self._conn is not None
        cur = self._conn.execute(
            "INSERT INTO memories(type, content, source, chat_id, created_ts, last_used_ts, applied_by)"
            " VALUES(?,?,?,?,?,?,?)",
            (type, content, source, chat_id, created_ts, last_used_ts, applied_by),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def memory_recall(
        self,
        query: str | None = None,
        chat_id: int | None = None,
        type: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        assert self._conn is not None
        params: list[Any] = []
        if query is not None:
            sql = (
                "SELECT m.id, m.type, m.content, m.source, m.chat_id,"
                " m.created_ts, m.last_used_ts, m.applied_by"
                " FROM memories_fts fts"
                " JOIN memories m ON m.id = fts.rowid"
                " WHERE memories_fts MATCH ?"
            )
            params.append(query)
            if chat_id is not None:
                sql += " AND (m.chat_id=? OR m.chat_id IS NULL)"
                params.append(chat_id)
            if type is not None:
                sql += " AND m.type=?"
                params.append(type)
            sql += " LIMIT ?"
            params.append(limit)
        else:
            sql = (
                "SELECT id, type, content, source, chat_id,"
                " created_ts, last_used_ts, applied_by"
                " FROM memories WHERE 1=1"
            )
            if chat_id is not None:
                sql += " AND (chat_id=? OR chat_id IS NULL)"
                params.append(chat_id)
            if type is not None:
                sql += " AND type=?"
                params.append(type)
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ----- Cursor -----

    def cursor_get(self, daemon_name: str) -> int:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT last_message_id FROM learn_cursor WHERE daemon_name=?",
            (daemon_name,),
        ).fetchone()
        return int(row["last_message_id"]) if row is not None else 0

    def cursor_set(self, daemon_name: str, last_message_id: int) -> None:
        assert self._conn is not None
        self._conn.execute(
            "INSERT OR REPLACE INTO learn_cursor(daemon_name, last_message_id) VALUES(?,?)",
            (daemon_name, last_message_id),
        )

    # ----- Messages since -----

    def messages_since(self, after_id: int) -> list[dict[str, Any]]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT id, chat_id, role, content, tokens, ts"
            " FROM messages WHERE id > ? ORDER BY id ASC",
            (after_id,),
        ).fetchall()
        return [dict(r) for r in rows]
