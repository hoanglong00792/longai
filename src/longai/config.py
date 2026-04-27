"""Config loader: tomllib + env. Boot-panic on missing required keys.

Lifted from dr-agent/internal/llm/openrouter.go:60-73 — panic at boot, not on
first message.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass
class BudgetCaps:
    global_daily_usd: float = 1.00
    per_user_daily_usd: float = 0.25
    per_call_max_usd: float = 0.025
    per_call_wall_clock_s: int = 30
    per_call_max_turns: int = 5


@dataclass
class Config:
    openrouter_api_key: str
    openrouter_base_url: str
    telegram_bot_token: str
    allowed_chat_ids: list[int]
    allowed_outbound_chat_ids: list[int]
    models: list[str]
    caps: BudgetCaps
    db_path: str
    mcp_config_path: str
    skill_repos: dict[str, str]
    log_level: str = "INFO"
    trace_dir: str | None = None


def _resolve_secret(env_name: str) -> str | None:
    """Env value, or path-to-file via {env_name}_FILE, or None."""
    v = os.environ.get(env_name)
    if v:
        return v.strip()
    file_env = os.environ.get(f"{env_name}_FILE")
    if file_env:
        try:
            return Path(file_env).expanduser().read_text().strip()
        except OSError as e:
            raise ConfigError(f"{env_name}_FILE unreadable: {e}") from e
    return None


def load(path: str = "~/.longai/config.toml", *, require_telegram: bool = True) -> Config:
    p = Path(path).expanduser()
    if not p.exists():
        raise ConfigError(f"config not found: {p}")
    raw = tomllib.loads(p.read_text())

    api_key = _resolve_secret("OPENROUTER_API_KEY")
    if not api_key:
        raise ConfigError(
            "OPENROUTER_API_KEY (or OPENROUTER_API_KEY_FILE) not set — exiting"
        )

    tg_token = _resolve_secret("TELEGRAM_BOT_TOKEN") or ""
    if require_telegram and not tg_token:
        raise ConfigError("TELEGRAM_BOT_TOKEN not set — exiting")

    caps_raw = raw.get("caps", {})
    paths_raw = raw.get("paths", {})
    skills_raw = raw.get("skills", {})
    logging_raw = raw.get("logging", {})
    models_raw = raw.get("models_refresh", {})

    # Resolve the model chain. Order:
    #   1. Cache (fresh, per refresh policy)
    #   2. OpenRouter live refresh
    #   3. Stale cache (refresh failed)
    #   4. Static `models = [...]` from this config file
    #   5. Last-resort minimal chain
    static_chain = list(raw.get("models", []) or [])
    refresh_policy = str(models_raw.get("policy", "weekly"))
    cache_path = str(models_raw.get(
        "cache_path", "~/.longai/models_cache.json"
    ))
    from longai.models_cache import resolve_chain
    resolved_models = resolve_chain(
        policy=refresh_policy,
        cache_path=cache_path,
        static_fallback=static_chain,
    )

    return Config(
        openrouter_api_key=api_key,
        openrouter_base_url=os.environ.get(
            "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
        ),
        telegram_bot_token=tg_token,
        allowed_chat_ids=list(raw.get("allowed_chat_ids", []) or []),
        allowed_outbound_chat_ids=list(raw.get("allowed_outbound_chat_ids", []) or []),
        models=resolved_models,
        caps=BudgetCaps(
            global_daily_usd=float(caps_raw.get("global_daily_usd", 1.00)),
            per_user_daily_usd=float(caps_raw.get("per_user_daily_usd", 0.25)),
            per_call_max_usd=float(caps_raw.get("per_call_max_usd", 0.025)),
            per_call_wall_clock_s=int(caps_raw.get("per_call_wall_clock_s", 30)),
            per_call_max_turns=int(caps_raw.get("per_call_max_turns", 5)),
        ),
        db_path=str(paths_raw.get("db_path", "~/.longai/state.db")),
        mcp_config_path=str(paths_raw.get("mcp_config_path", "~/.longai/mcp.json")),
        skill_repos={k: os.path.expanduser(str(v)) for k, v in skills_raw.items()},
        log_level=str(logging_raw.get("level", "INFO")),
        trace_dir=os.environ.get("LONGAI_TRACE_DIR"),
    )
