"""Config loader: TOML + env, panic on missing required keys."""
import os
import textwrap

import pytest

from longai.config import Config, ConfigError, load


def test_panic_on_missing_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_FILE", raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text("allowed_chat_ids = []\n")
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        load(str(cfg), require_telegram=False)


def test_loads_with_api_key_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    cfg = tmp_path / "config.toml"
    # `[models_refresh] policy = "manual"` + non-existent cache_path forces
    # the loader to fall back to the static `models = [...]` list (no network,
    # no shared cache file), so the test stays hermetic.
    cfg.write_text(textwrap.dedent(f"""
        allowed_chat_ids = [12345]
        allowed_outbound_chat_ids = []
        models = ["x/y:free", "x/y"]
        [models_refresh]
        policy = "manual"
        cache_path = "{tmp_path}/no_such_cache.json"
        [caps]
        global_daily_usd = 1.00
        per_user_daily_usd = 0.25
        per_call_max_usd = 0.025
        per_call_wall_clock_s = 30
        per_call_max_turns = 5
        [paths]
        db_path = "/tmp/test.db"
        mcp_config_path = "/tmp/mcp.json"
        [skills]
        shared = "/tmp/shared"
        personal = "/tmp/personal"
        work = "/tmp/work"
        [logging]
        level = "INFO"
    """))
    c = load(str(cfg), require_telegram=False)
    assert c.openrouter_api_key == "sk-test"
    assert c.allowed_chat_ids == [12345]
    assert c.caps.per_call_max_usd == 0.025
    assert c.models[0] == "x/y:free"


def test_api_key_file_priority(tmp_path, monkeypatch):
    keyfile = tmp_path / "key.txt"
    keyfile.write_text("sk-fromfile\n")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY_FILE", str(keyfile))
    cfg = tmp_path / "config.toml"
    cfg.write_text("allowed_chat_ids = []\n")
    c = load(str(cfg), require_telegram=False)
    assert c.openrouter_api_key == "sk-fromfile"


def test_telegram_token_required_when_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text("allowed_chat_ids = []\n")
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load(str(cfg), require_telegram=True)
