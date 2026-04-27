"""Config loader: tomllib + env. Boot-panic on missing required keys.

Discipline: panic at boot, not on first message — surface bad config before
the bot is in production polling mode.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    pass


_TIERS = ("S", "M", "L")


@dataclass
class BudgetCaps:
    """I4 caps. Per-tier overrides (when set) shape multi-step capability without
    moving the dollar floor. Free models stay free regardless of tier."""
    global_daily_usd: float = 1.00
    per_user_daily_usd: float = 0.25
    per_call_max_usd: float = 0.025
    per_call_wall_clock_s: int = 30
    per_call_max_turns: int = 5
    # Per-tier overrides keyed by "S" | "M" | "L". Missing tier → falls back
    # to the base value. Stored as a flat dict-of-dicts for easy TOML round-trip.
    by_tier: dict[str, dict[str, float]] = field(default_factory=dict)

    def turns_for(self, tier: str) -> int:
        v = self.by_tier.get(tier, {}).get("per_call_max_turns")
        return int(v) if v is not None else self.per_call_max_turns

    def wall_clock_for(self, tier: str) -> int:
        v = self.by_tier.get(tier, {}).get("per_call_wall_clock_s")
        return int(v) if v is not None else self.per_call_wall_clock_s


@dataclass
class Config:
    openrouter_api_key: str
    openrouter_base_url: str
    telegram_bot_token: str
    allowed_chat_ids: list[int]
    allowed_outbound_chat_ids: list[int]
    models: list[str]
    # Tier-aware chains: {"S": [...], "M": [...], "L": [...], "fallback": [...]}.
    # Always populated — legacy `models = [...]` configs map to all tiers
    # sharing one chain, with the paid floor (last non-`:free` entry) as
    # the fallback chain.
    model_chains: dict[str, list[str]]
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

    caps_raw = raw.get("caps", {}) or {}
    paths_raw = raw.get("paths", {})
    skills_raw = raw.get("skills", {})
    logging_raw = raw.get("logging", {})
    models_refresh_raw = raw.get("models_refresh", {})

    # Per-tier caps overrides (optional). TOML format:
    #   [caps.tier_L]
    #   per_call_max_turns = 12
    #   per_call_wall_clock_s = 90
    by_tier_caps: dict[str, dict[str, float]] = {}
    for t in _TIERS:
        sub = caps_raw.get(f"tier_{t}")
        if isinstance(sub, dict):
            by_tier_caps[t] = {
                k: float(v) for k, v in sub.items()
                if k in ("per_call_max_turns", "per_call_wall_clock_s",
                         "per_call_max_usd")
            }

    # Model chains. Two valid TOML shapes:
    #
    #   (a) Tiered:  [models.tier_S].chain = [...] etc, plus [models.fallback].chain
    #   (b) Legacy:  models = [flat list with paid floor at end]
    #
    # Tomllib distinguishes them by node type — a list at `models` is legacy,
    # a table at `models` is tiered.
    models_node = raw.get("models")
    if isinstance(models_node, dict):
        is_tiered = any(
            isinstance(models_node.get(f"tier_{t}"), dict) for t in _TIERS
        )
        if not is_tiered:
            raise ConfigError(
                "[models] table present but no [models.tier_S/M/L] subsections found"
            )
        chains: dict[str, list[str]] = {}
        for t in _TIERS:
            sub = models_node.get(f"tier_{t}", {}) or {}
            chains[t] = list(sub.get("chain", []) or [])
        fb = models_node.get("fallback", {}) or {}
        chains["fallback"] = list(fb.get("chain", []) or [])
        # Tiered configs bypass the cache for now (PR B integrates the
        # tier-aware refresh). The "default chain" for legacy callers /
        # dryrun display is M's chain plus fallback.
        resolved_models = chains["M"] + chains["fallback"]
    elif isinstance(models_node, list) or models_node is None:
        # Legacy flat chain — runs through the cache-aware resolver.
        static_chain = list(models_node or [])
        refresh_policy = str(models_refresh_raw.get("policy", "weekly"))
        cache_path = str(models_refresh_raw.get(
            "cache_path", "~/.longai/models_cache.json"
        ))
        from longai.models_cache import resolve_chain
        resolved_models = resolve_chain(
            policy=refresh_policy,
            cache_path=cache_path,
            static_fallback=static_chain,
        )
        # Backward compat: every tier shares the full chain, fallback empty.
        # The trailing paid floor is already in `resolved_models`.
        chains = {
            "S": list(resolved_models), "M": list(resolved_models),
            "L": list(resolved_models), "fallback": [],
        }
    else:
        raise ConfigError(
            f"unexpected type for `models`: {type(models_node).__name__}"
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
        model_chains=chains,
        caps=BudgetCaps(
            global_daily_usd=float(caps_raw.get("global_daily_usd", 1.00)),
            per_user_daily_usd=float(caps_raw.get("per_user_daily_usd", 0.25)),
            per_call_max_usd=float(caps_raw.get("per_call_max_usd", 0.025)),
            per_call_wall_clock_s=int(caps_raw.get("per_call_wall_clock_s", 30)),
            per_call_max_turns=int(caps_raw.get("per_call_max_turns", 5)),
            by_tier=by_tier_caps,
        ),
        db_path=str(paths_raw.get("db_path", "~/.longai/state.db")),
        mcp_config_path=str(paths_raw.get("mcp_config_path", "~/.longai/mcp.json")),
        skill_repos={k: os.path.expanduser(str(v)) for k, v in skills_raw.items()},
        log_level=str(logging_raw.get("level", "INFO")),
        trace_dir=os.environ.get("LONGAI_TRACE_DIR"),
    )
