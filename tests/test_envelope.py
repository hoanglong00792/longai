"""I9 enforcement — token rename happens in envelope, on every path."""
from longai.envelope import format_error, format_result


class _FakeResult:
    text = "hello world"
    prompt_tokens = 100
    completion_tokens = 50
    spend_usd = 0.0012
    turns = 2
    stopped = "final"
    error = None


def test_envelope_renames_prompt_to_input_tokens():
    out = format_result(_FakeResult(), model="x/y", trace_id="trace-1")
    assert out["usage"]["input_tokens"] == 100
    assert out["usage"]["output_tokens"] == 50
    assert "prompt_tokens" not in out["usage"]
    assert "completion_tokens" not in out["usage"]


def test_envelope_carries_all_fields():
    out = format_result(_FakeResult(), model="x/y", trace_id="trace-1")
    assert out["result"] == "hello world"
    assert out["model"] == "x/y"
    assert out["turns"] == 2
    assert out["stopped"] == "final"
    assert out["spend_usd"] == 0.0012
    assert out["trace_id"] == "trace-1"
    assert out["error"] is None


def test_envelope_error_path_also_renames():
    """I9: rename must apply on EVERY error path, not just happy."""
    class _ErrResult:
        text = ""
        prompt_tokens = 10
        completion_tokens = 0
        spend_usd = 0.0001
        turns = 1
        stopped = "budget"
        error = "per-user cap reached"

    out = format_result(_ErrResult(), model="x/y", trace_id="t-2")
    assert out["usage"]["input_tokens"] == 10
    assert out["usage"]["output_tokens"] == 0
    assert out["stopped"] == "budget"
    assert out["error"] == "per-user cap reached"


def test_format_error_envelope():
    out = format_error(ValueError("boom"), trace_id="t-3")
    assert out["stopped"] == "error"
    assert "boom" in out["error"]
    assert out["usage"]["input_tokens"] == 0
    assert out["usage"]["output_tokens"] == 0
