"""Tracer — file-based dump per run, including timing JSONL."""
import json

from longai.trace import Tracer


def test_no_op_when_no_dir():
    """Tracer with None dir silently no-ops on every method."""
    t = Tracer(None)
    t.timing("chat", 123.4, model="x")  # must not raise
    t.input("hi")
    t.system("sys")
    t.output({"k": "v"})
    assert t.run_id  # still has a run id


def test_timing_appends_jsonl(tmp_path):
    t = Tracer(str(tmp_path))
    t.timing("chat", 1234.56, model="gemma:free", turn=1)
    t.timing("tool", 250.1, name="coingecko_token_info", turn=1)
    # The tracer creates its own subdir; find the timings file
    [run_dir] = list(tmp_path.iterdir())
    timings_file = run_dir / "06_timings.jsonl"
    assert timings_file.exists()
    lines = timings_file.read_text().strip().splitlines()
    assert len(lines) == 2
    rec1 = json.loads(lines[0])
    assert rec1["phase"] == "chat"
    assert rec1["ms"] == 1234.6  # rounded to 1 decimal
    assert rec1["model"] == "gemma:free"
    assert rec1["turn"] == 1
    assert "ts" in rec1
    rec2 = json.loads(lines[1])
    assert rec2["name"] == "coingecko_token_info"


def test_timing_rounds_to_one_decimal(tmp_path):
    t = Tracer(str(tmp_path))
    t.timing("x", 1234.56789)
    [run_dir] = list(tmp_path.iterdir())
    rec = json.loads((run_dir / "06_timings.jsonl").read_text().strip())
    assert rec["ms"] == 1234.6


def test_other_files_still_work_with_timing(tmp_path):
    """Adding timing didn't break input/system/output."""
    t = Tracer(str(tmp_path))
    t.input("hello")
    t.system("you are X")
    t.output({"result": "ok"})
    t.timing("chat", 100.0)
    [run_dir] = list(tmp_path.iterdir())
    assert (run_dir / "01_input.txt").read_text() == "hello"
    assert (run_dir / "02_system_prompt.txt").read_text() == "you are X"
    assert (run_dir / "07_output.json").exists()
    assert (run_dir / "06_timings.jsonl").exists()
