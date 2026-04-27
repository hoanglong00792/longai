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
