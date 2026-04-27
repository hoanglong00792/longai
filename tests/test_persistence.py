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
