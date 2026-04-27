# tests/test_cli.py
"""CLI subcommands."""
import json
import subprocess
import sys
from unittest.mock import AsyncMock, patch

import pytest


def _run(args: list[str], env_extra: dict | None = None, **kw):
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": "src"}
    import os
    env.update(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "longai", *args],
        capture_output=True, text=True, env=env, **kw,
    )


def test_help_runs_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_FILE", raising=False)
    r = _run(["--help"])
    assert r.returncode == 0
    assert "longai" in r.stdout.lower()


def test_dryrun_panics_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text("allowed_chat_ids = []\n")
    r = _run(["dryrun", "--config", str(cfg)])
    assert r.returncode != 0
    assert "OPENROUTER_API_KEY" in r.stderr or "OPENROUTER_API_KEY" in r.stdout


# ── Output mode resolver (PR F) ────────────────────────────────────────


class _Args:
    """Minimal stand-in for argparse.Namespace."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_output_mode_explicit_json_wins(monkeypatch):
    from longai.cli import _resolve_output_mode
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    args = _Args(output_json=True, output_text=False)
    assert _resolve_output_mode(args) == "json"


def test_output_mode_explicit_text_wins(monkeypatch):
    from longai.cli import _resolve_output_mode
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    args = _Args(output_json=False, output_text=True)
    assert _resolve_output_mode(args) == "text"


def test_output_mode_default_text_when_tty(monkeypatch):
    from longai.cli import _resolve_output_mode
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    args = _Args(output_json=False, output_text=False)
    assert _resolve_output_mode(args) == "text"


def test_output_mode_default_json_when_piped(monkeypatch):
    from longai.cli import _resolve_output_mode
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    args = _Args(output_json=False, output_text=False)
    assert _resolve_output_mode(args) == "json"


def test_emit_text_writes_just_result():
    """In text mode, only the `result` field reaches stdout."""
    import io
    from contextlib import redirect_stdout
    from longai.cli import _emit
    buf = io.StringIO()
    with redirect_stdout(buf):
        _emit({"result": "hello", "model": "x", "spend_usd": 0.01, "error": None}, "text")
    assert buf.getvalue().strip() == "hello"


def test_emit_json_writes_full_envelope():
    import io
    from contextlib import redirect_stdout
    from longai.cli import _emit
    buf = io.StringIO()
    env = {"result": "hello", "model": "x", "tier": "M", "spend_usd": 0.01, "error": None}
    with redirect_stdout(buf):
        _emit(env, "json")
    parsed = json.loads(buf.getvalue())
    assert parsed["result"] == "hello"
    assert parsed["model"] == "x"


def test_emit_text_error_goes_to_stderr():
    import io
    from contextlib import redirect_stdout, redirect_stderr
    from longai.cli import _emit
    out_buf, err_buf = io.StringIO(), io.StringIO()
    env = {"result": "Sorry — boom", "error": "boom", "model": ""}
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        _emit(env, "text")
    assert out_buf.getvalue() == ""           # nothing on stdout for errors
    assert "Sorry" in err_buf.getvalue()
