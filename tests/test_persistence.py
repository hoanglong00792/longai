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
            "learn_cursor", "traces"}.issubset(tables)


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
