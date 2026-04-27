import pytest

from longai.persistence import Persistence


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
