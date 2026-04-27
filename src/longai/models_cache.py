"""Free-models discovery + cache layer.

At bot/CLI startup, the chain of free tool-capable models is loaded from a
cache file (~/.longai/models_cache.json by default). If the cache is older
than the configured TTL, it's refreshed against the OpenRouter API. If the
refresh fails (offline, API down), the stale cache OR the static fallback
chain in config.toml is used — the bot never fails to start over models.

Refresh policies (in order of fastness):
  - "never"   — never refresh; always use static config or current cache
  - "manual"  — only refresh when `longai refresh` is invoked
  - "monthly" — refresh if cache is older than 30 days
  - "weekly"  — refresh if cache is older than 7 days  (default)
  - "daily"   — refresh if cache is older than 1 day
  - "startup" — refresh on every CLI/bot start

Doyen's curated priority — preferred families and capable models first;
small/fast fallbacks; meta-router last; paid floor at the very end.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Doyen's preferred order. Models not in this list go after these in
# alphabetical order. Models in SKIP are excluded entirely (poor tool-use).
PRIORITY = [
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
    "minimax/minimax-m2.5:free",
    "tencent/hy3-preview:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "inclusionai/ling-2.6-flash:free",
    "inclusionai/ling-2.6-1t:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "qwen/qwen3-coder:free",
    "openai/gpt-oss-120b:free",
    "openai/gpt-oss-20b:free",
    "z-ai/glm-4.5-air:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "liquid/lfm-2.5-1.2b-instruct:free",
    "liquid/lfm-2.5-1.2b-thinking:free",
    "openrouter/free",  # meta-router — last-chance free
]
# Models that claim tool-call support but actually fail in agent loops.
SKIP = {
    "baidu/qianfan-ocr-fast:free",  # OCR-specialized
}
PAID_FLOOR = "google/gemma-4-26b-a4b-it"

# Refresh TTLs in seconds
TTL_S = {
    "startup": 0,            # always stale → refresh
    "daily": 86_400,
    "weekly": 7 * 86_400,
    "monthly": 30 * 86_400,
    "manual": float("inf"),  # never auto-refresh
    "never": float("inf"),
}


@dataclass
class CacheEntry:
    refreshed_ts: int
    slugs: list[str]      # the ordered chain (free + paid floor)
    raw_count: int        # number of free tool-capable models discovered


def _cache_path(path: str) -> Path:
    return Path(os.path.expanduser(path))


def load_cache(path: str) -> CacheEntry | None:
    """Load the cache file, or None if missing/malformed."""
    p = _cache_path(path)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        return CacheEntry(
            refreshed_ts=int(d["refreshed_ts"]),
            slugs=list(d["slugs"]),
            raw_count=int(d.get("raw_count", len(d["slugs"]))),
        )
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning("models_cache: failed to load %s: %s", p, e)
        return None


def save_cache(path: str, entry: CacheEntry) -> None:
    p = _cache_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "refreshed_ts": entry.refreshed_ts,
        "slugs": entry.slugs,
        "raw_count": entry.raw_count,
    }, indent=2) + "\n")


def is_stale(entry: CacheEntry, policy: str, *, now_ts: int | None = None) -> bool:
    """True if the cache should be refreshed under the given policy."""
    ttl = TTL_S.get(policy, TTL_S["weekly"])
    age = (now_ts if now_ts is not None else int(time.time())) - entry.refreshed_ts
    return age >= ttl


def fetch_from_openrouter(timeout_s: float = 15.0) -> list[str]:
    """Query OpenRouter, filter to free tool-capable, order, append paid floor.

    Imports httpx lazily so the module is importable without the dep installed
    (relevant for tests and offline CI).
    """
    import httpx
    resp = httpx.get("https://openrouter.ai/api/v1/models", timeout=timeout_s)
    resp.raise_for_status()
    data = resp.json()

    free_tool: list[str] = []
    for m in data.get("data", []):
        pricing = m.get("pricing", {})
        if pricing.get("prompt") != "0":
            continue
        if "tools" not in (m.get("supported_parameters") or []):
            continue
        slug = m.get("id")
        if slug and slug not in SKIP:
            free_tool.append(slug)

    # Order: PRIORITY first (preserving order), then unknown free models alphabetically
    seen = set()
    ordered: list[str] = []
    for s in PRIORITY:
        if s in free_tool:
            ordered.append(s); seen.add(s)
    for s in sorted(free_tool):
        if s not in seen:
            ordered.append(s); seen.add(s)
    ordered.append(PAID_FLOOR)
    return ordered


def refresh(path: str) -> CacheEntry | None:
    """Fetch from OR + write cache. Returns None on failure (network, etc)."""
    try:
        slugs = fetch_from_openrouter()
    except Exception as e:
        logger.warning("models_cache: refresh failed (%s); will use stale/static fallback", e)
        return None
    entry = CacheEntry(
        refreshed_ts=int(time.time()),
        slugs=slugs,
        raw_count=len(slugs) - 1,  # exclude paid floor
    )
    save_cache(path, entry)
    logger.info("models_cache: refreshed; %d free + 1 paid → %s",
                entry.raw_count, path)
    return entry


def resolve_chain(
    *,
    policy: str,
    cache_path: str,
    static_fallback: list[str],
) -> list[str]:
    """The single function called at config.load().

    Order of preference:
        1. Fresh cache (within TTL) → use as-is, no network
        2. Stale cache + successful refresh → use refreshed
        3. Stale cache + refresh failed → use stale cache anyway (with warning)
        4. No cache + successful refresh → use refreshed
        5. No cache + refresh failed → fall back to static config list
    """
    cache = load_cache(cache_path)

    # Path: never/manual + cache exists → use it (regardless of age)
    if policy in ("never", "manual") and cache is not None:
        return cache.slugs

    # Path: never/manual + no cache → static fallback (DO NOT touch network)
    if policy in ("never", "manual"):
        if static_fallback:
            return static_fallback
        # else fall through to last-resort minimal chain below

    # Path: cache exists and fresh → use it
    if cache is not None and not is_stale(cache, policy):
        return cache.slugs

    # Path: refresh from network
    refreshed = refresh(cache_path)
    if refreshed is not None:
        return refreshed.slugs

    # Path: refresh failed; use stale cache if we have one
    if cache is not None:
        logger.warning("models_cache: using STALE cache (refresh failed)")
        return cache.slugs

    # Path: nothing; use static config list
    if static_fallback:
        logger.warning("models_cache: no cache and refresh failed; using static config list")
        return static_fallback

    # Last-resort minimal chain
    logger.warning("models_cache: nothing available; emitting minimal chain")
    return [
        "google/gemma-4-26b-a4b-it:free",
        PAID_FLOOR,
    ]
