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
