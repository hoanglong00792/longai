import sqlite3

import pytest

from longai.persistence import BudgetExceeded, Persistence


def test_schema_creates_all_tables(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    tables = {row[0] for row in p._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {"messages", "debits", "cooldowns", "memories",
            "learn_cursor", "traces", "model_stats"}.issubset(tables)


def test_append_and_load_history(tmp_db_path, fixed_now_ts):
    p = Persistence(tmp_db_path)
    p.init()
    p.append_message(123, "user", "hello", tokens=5, ts=fixed_now_ts)
    p.append_message(123, "assistant", "hi", tokens=3, ts=fixed_now_ts + 1)
    p.append_message(456, "user", "other chat", tokens=4, ts=fixed_now_ts + 2)

    hist = p.load_history(123, max_msgs=10, max_tokens=1000)
    assert len(hist) == 2
    assert hist[0]["content"] == "hello"
    assert hist[1]["content"] == "hi"
    # Other chat's history not leaked
    assert all("other chat" not in m["content"] for m in hist)


def test_load_history_respects_max_msgs(tmp_db_path, fixed_now_ts):
    p = Persistence(tmp_db_path)
    p.init()
    for i in range(30):
        p.append_message(1, "user", f"msg{i}", tokens=1, ts=fixed_now_ts + i)
    hist = p.load_history(1, max_msgs=10, max_tokens=10000)
    assert len(hist) == 10
    # Sliding window: most recent 10
    assert hist[-1]["content"] == "msg29"


def test_load_history_respects_max_tokens(tmp_db_path, fixed_now_ts):
    p = Persistence(tmp_db_path)
    p.init()
    for i in range(20):
        p.append_message(1, "user", f"m{i}", tokens=100, ts=fixed_now_ts + i)
    hist = p.load_history(1, max_msgs=100, max_tokens=550)  # ~5 messages
    assert len(hist) == 5


def test_debit_within_caps_succeeds(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    user_total, global_total = p.debit(
        chat_id=1, model="m", usd=0.01, day_utc="2026-04-27",
        per_user_cap=0.25, global_cap=1.00, ts=1000,
    )
    assert user_total == pytest.approx(0.01)
    assert global_total == pytest.approx(0.01)


def test_debit_per_user_cap_raises(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    p.debit(chat_id=1, model="m", usd=0.20,
            day_utc="2026-04-27", per_user_cap=0.25, global_cap=10, ts=1)
    with pytest.raises(BudgetExceeded) as exc:
        p.debit(chat_id=1, model="m", usd=0.10,
                day_utc="2026-04-27", per_user_cap=0.25, global_cap=10, ts=2)
    assert exc.value.scope == "per_user"


def test_debit_global_cap_raises(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    p.debit(chat_id=1, model="m", usd=0.05,
            day_utc="2026-04-27", per_user_cap=10, global_cap=0.10, ts=1)
    with pytest.raises(BudgetExceeded) as exc:
        p.debit(chat_id=2, model="m", usd=0.10,
                day_utc="2026-04-27", per_user_cap=10, global_cap=0.10, ts=2)
    assert exc.value.scope == "global"


def test_debit_other_user_unblocked_by_first_users_cap(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    # User 1 hits per-user cap
    p.debit(chat_id=1, model="m", usd=0.25,
            day_utc="2026-04-27", per_user_cap=0.25, global_cap=10, ts=1)
    # User 2 should still go through
    user_total, _ = p.debit(chat_id=2, model="m", usd=0.10,
                             day_utc="2026-04-27", per_user_cap=0.25, global_cap=10, ts=2)
    assert user_total == pytest.approx(0.10)


# ----- Task 7 tests -----

def test_cooldowns_set_and_query(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    p.set_cooldown("gpt-4", until_ts=2000)
    p.set_cooldown("gemma", until_ts=500)
    cooled = p.cooled_models(now_ts=1000)
    assert "gpt-4" in cooled
    assert "gemma" not in cooled


def test_set_cooldown_upsert(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    p.set_cooldown("gpt-4", until_ts=2000)
    p.set_cooldown("gpt-4", until_ts=500)  # upsert to earlier time
    cooled = p.cooled_models(now_ts=1000)
    assert "gpt-4" not in cooled


def test_log_trace(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    p.log_trace(
        run_id="run-abc",
        chat_id=1,
        started_ts=1000,
        stopped="ok",
        spend_usd=0.01,
        turns=3,
        error=None,
    )
    row = p._conn.execute(
        "SELECT * FROM traces WHERE run_id='run-abc'"
    ).fetchone()
    assert row is not None
    assert row["chat_id"] == 1
    assert row["turns"] == 3
    assert row["spend_usd"] == pytest.approx(0.01)


def test_memory_save_and_recall(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    rowid = p.memory_save(
        type="fact",
        content="The sky is blue",
        source="user",
        chat_id=42,
        created_ts=1000,
        applied_by="user",
    )
    assert isinstance(rowid, int) and rowid > 0

    # FTS search
    results = p.memory_recall(query="sky", chat_id=42)
    assert len(results) == 1
    assert results[0]["content"] == "The sky is blue"

    # Plain select by type
    results2 = p.memory_recall(type="fact", chat_id=42)
    assert len(results2) == 1

    # global memory (chat_id=None) visible to any chat
    p.memory_save(
        type="fact",
        content="Water is wet",
        source="agent",
        chat_id=None,
        created_ts=1001,
        applied_by="agent",
    )
    results3 = p.memory_recall(query="wet", chat_id=99)
    assert len(results3) == 1
    assert results3[0]["content"] == "Water is wet"


def test_memory_save_dedups_on_chat_id_type_content(tmp_db_path):
    """Saving the same (chat_id, type, content) twice returns the same id."""
    p = Persistence(tmp_db_path)
    p.init()
    id1 = p.memory_save(
        type="preference", content="prefers Base",
        source="user", chat_id=42, created_ts=1, applied_by="user",
    )
    id2 = p.memory_save(
        type="preference", content="prefers Base",
        source="user", chat_id=42, created_ts=2, applied_by="user",
    )
    assert id1 == id2
    rows = p._conn.execute(
        "SELECT COUNT(*) FROM memories WHERE chat_id=42 AND content='prefers Base'"
    ).fetchone()
    assert rows[0] == 1


def test_memory_save_dedups_global_null_chat_id(tmp_db_path):
    """NULL chat_id should also dedup (uses SQL IS comparison)."""
    p = Persistence(tmp_db_path)
    p.init()
    id1 = p.memory_save(
        type="preference", content="global rule",
        source="agent", chat_id=None, created_ts=1, applied_by="user",
    )
    id2 = p.memory_save(
        type="preference", content="global rule",
        source="agent", chat_id=None, created_ts=2, applied_by="user",
    )
    assert id1 == id2


def test_memory_save_distinguishes_different_chat_ids(tmp_db_path):
    """Same content under different chat_ids are independent."""
    p = Persistence(tmp_db_path)
    p.init()
    id1 = p.memory_save(
        type="preference", content="X",
        source="user", chat_id=1, created_ts=1, applied_by="user",
    )
    id2 = p.memory_save(
        type="preference", content="X",
        source="user", chat_id=2, created_ts=1, applied_by="user",
    )
    assert id1 != id2


def test_migrate_collapses_existing_duplicate_rows(tmp_db_path):
    """Migration runs at init() and removes duplicate rows from legacy DBs."""
    p = Persistence(tmp_db_path)
    p.init()
    # Bypass dedup helper and write raw duplicates simulating a legacy DB
    p._conn.execute(
        "INSERT INTO memories(type, content, source, chat_id, created_ts, applied_by)"
        " VALUES('preference', 'dup-content', 'user', NULL, 1, 'user')"
    )
    p._conn.execute(
        "INSERT INTO memories(type, content, source, chat_id, created_ts, applied_by)"
        " VALUES('preference', 'dup-content', 'user', NULL, 2, 'user')"
    )
    p._conn.execute(
        "INSERT INTO memories(type, content, source, chat_id, created_ts, applied_by)"
        " VALUES('preference', 'dup-content', 'user', NULL, 3, 'user')"
    )
    assert p._conn.execute(
        "SELECT COUNT(*) FROM memories WHERE content='dup-content'"
    ).fetchone()[0] == 3
    # Re-run migrate; should collapse to 1
    p._migrate()
    assert p._conn.execute(
        "SELECT COUNT(*) FROM memories WHERE content='dup-content'"
    ).fetchone()[0] == 1


def test_memory_applied_by_check_constraint(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    with pytest.raises(sqlite3.IntegrityError):
        p.memory_save(
            type="fact",
            content="bad actor",
            source="daemon",
            chat_id=None,
            created_ts=1000,
            applied_by="daemon",  # violates CHECK constraint
        )


def test_cursor_get_set(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    # default is 0 when not set
    assert p.cursor_get("learner") == 0
    p.cursor_set("learner", 42)
    assert p.cursor_get("learner") == 42
    # upsert
    p.cursor_set("learner", 100)
    assert p.cursor_get("learner") == 100


def test_record_attempt_success_seeds_ewma(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    p.record_attempt(model="m1", outcome="success", latency_ms=200.0, now_ts=1000)
    row = p._conn.execute(
        "SELECT n_success, n_error, ewma_latency_ms, last_outcome, last_ts"
        " FROM model_stats WHERE model='m1'"
    ).fetchone()
    assert row["n_success"] == 1
    assert row["n_error"] == 0
    assert row["ewma_latency_ms"] == pytest.approx(200.0)
    assert row["last_outcome"] == "success"
    assert row["last_ts"] == 1000


def test_record_attempt_failure_increments_error_no_ewma(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    p.record_attempt(model="m1", outcome="timeout", latency_ms=30000.0, now_ts=1000)
    row = p._conn.execute(
        "SELECT n_success, n_error, ewma_latency_ms FROM model_stats WHERE model='m1'"
    ).fetchone()
    assert row["n_success"] == 0
    assert row["n_error"] == 1
    assert row["ewma_latency_ms"] is None  # failures don't seed ewma


def test_record_attempt_ewma_blends_subsequent_successes(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    p.record_attempt(model="m1", outcome="success", latency_ms=1000.0, now_ts=1)
    p.record_attempt(model="m1", outcome="success", latency_ms=2000.0, now_ts=2)
    row = p._conn.execute(
        "SELECT ewma_latency_ms FROM model_stats WHERE model='m1'"
    ).fetchone()
    # alpha=0.3 → 0.3*2000 + 0.7*1000 = 1300
    assert row["ewma_latency_ms"] == pytest.approx(1300.0)


def test_record_attempt_failure_after_success_keeps_ewma(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    p.record_attempt(model="m1", outcome="success", latency_ms=500.0, now_ts=1)
    p.record_attempt(model="m1", outcome="rate_limit", latency_ms=12.0, now_ts=2)
    row = p._conn.execute(
        "SELECT n_success, n_error, ewma_latency_ms, last_outcome FROM model_stats"
        " WHERE model='m1'"
    ).fetchone()
    assert row["n_success"] == 1
    assert row["n_error"] == 1
    assert row["ewma_latency_ms"] == pytest.approx(500.0)  # untouched
    assert row["last_outcome"] == "rate_limit"


def test_record_attempt_consecutive_failures_increments_and_resets(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    # 3 failures in a row → cf goes 0 → 1 → 2 → 3
    p.record_attempt(model="m", outcome="rate_limit", latency_ms=1, now_ts=1)
    p.record_attempt(model="m", outcome="rate_limit", latency_ms=1, now_ts=2)
    p.record_attempt(model="m", outcome="timeout", latency_ms=1, now_ts=3)
    row = p._conn.execute(
        "SELECT consecutive_failures FROM model_stats WHERE model='m'"
    ).fetchone()
    assert row["consecutive_failures"] == 3
    # Success resets to 0
    p.record_attempt(model="m", outcome="success", latency_ms=100, now_ts=4)
    row = p._conn.execute(
        "SELECT consecutive_failures FROM model_stats WHERE model='m'"
    ).fetchone()
    assert row["consecutive_failures"] == 0


def test_model_stats_for_returns_only_requested_models(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    p.record_attempt(model="a", outcome="success", latency_ms=100, now_ts=1)
    p.record_attempt(model="b", outcome="rate_limit", latency_ms=1, now_ts=2)
    p.record_attempt(model="c", outcome="success", latency_ms=200, now_ts=3)
    out = p.model_stats_for(["a", "b", "missing"])
    assert set(out.keys()) == {"a", "b"}  # missing absent
    assert out["a"]["n_success"] == 1 and out["a"]["n_error"] == 0
    assert out["b"]["n_success"] == 0 and out["b"]["n_error"] == 1
    assert "c" not in out


def test_model_stats_for_empty_input(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    assert p.model_stats_for([]) == {}


def test_init_is_idempotent_for_consecutive_failures_migration(tmp_db_path):
    """Existing DB without the column should auto-add it on next init()."""
    p = Persistence(tmp_db_path)
    p.init()
    # Simulate a pre-migration DB by dropping the column. SQLite has no
    # DROP COLUMN before 3.35; instead, recreate the table without it.
    p._conn.executescript("""
        DROP TABLE model_stats;
        CREATE TABLE model_stats (
            model TEXT PRIMARY KEY,
            n_success INTEGER NOT NULL DEFAULT 0,
            n_error INTEGER NOT NULL DEFAULT 0,
            ewma_latency_ms REAL,
            last_outcome TEXT,
            last_ts INTEGER
        );
    """)
    p._conn.execute(
        "INSERT INTO model_stats(model, n_success, n_error, last_outcome, last_ts)"
        " VALUES('legacy', 5, 0, 'success', 100)"
    )
    p.close()
    # Re-open: migration should add the column without dropping data.
    p2 = Persistence(tmp_db_path)
    p2.init()
    cols = {r[1] for r in p2._conn.execute("PRAGMA table_info(model_stats)")}
    assert "consecutive_failures" in cols
    row = p2._conn.execute("SELECT * FROM model_stats WHERE model='legacy'").fetchone()
    assert row["n_success"] == 5
    assert row["consecutive_failures"] == 0  # backfilled to default
    # Calling init() again is a no-op
    p2._migrate()
    cols2 = {r[1] for r in p2._conn.execute("PRAGMA table_info(model_stats)")}
    assert cols2 == cols


def test_messages_since(tmp_db_path, fixed_now_ts):
    p = Persistence(tmp_db_path)
    p.init()
    p.append_message(1, "user", "first", tokens=1, ts=fixed_now_ts)
    p.append_message(1, "assistant", "second", tokens=1, ts=fixed_now_ts + 1)
    p.append_message(2, "user", "other", tokens=1, ts=fixed_now_ts + 2)

    # grab all IDs so we can use the first one as after_id
    all_rows = p._conn.execute("SELECT id FROM messages ORDER BY id").fetchall()
    first_id = all_rows[0]["id"]

    msgs = p.messages_since(after_id=first_id)
    assert len(msgs) == 2  # second and other, but not first
    assert msgs[0]["content"] == "second"
    assert msgs[1]["content"] == "other"
