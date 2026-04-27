"""Tests for models_cache — load/save/staleness/resolve_chain."""
import json
import time

import pytest

from longai.models_cache import (
    CacheEntry,
    PAID_FLOOR,
    is_stale,
    load_cache,
    resolve_chain,
    save_cache,
)


def test_load_cache_missing_returns_none(tmp_path):
    assert load_cache(str(tmp_path / "no.json")) is None


def test_save_and_load_roundtrip(tmp_path):
    p = tmp_path / "cache.json"
    e = CacheEntry(refreshed_ts=1000, slugs=["a:free", "b"], raw_count=1)
    save_cache(str(p), e)
    loaded = load_cache(str(p))
    assert loaded is not None
    assert loaded.refreshed_ts == 1000
    assert loaded.slugs == ["a:free", "b"]
    assert loaded.raw_count == 1


def test_load_cache_malformed_returns_none(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    assert load_cache(str(p)) is None


def test_is_stale_weekly():
    now = 1_000_000
    fresh = CacheEntry(refreshed_ts=now - 3 * 86400, slugs=[], raw_count=0)  # 3 days
    old = CacheEntry(refreshed_ts=now - 8 * 86400, slugs=[], raw_count=0)    # 8 days
    assert not is_stale(fresh, "weekly", now_ts=now)
    assert is_stale(old, "weekly", now_ts=now)


def test_is_stale_startup_always_stale():
    e = CacheEntry(refreshed_ts=1, slugs=[], raw_count=0)
    assert is_stale(e, "startup", now_ts=2)


def test_is_stale_never_or_manual_never_stale():
    now = 1_000_000_000
    very_old = CacheEntry(refreshed_ts=1, slugs=[], raw_count=0)
    assert not is_stale(very_old, "never", now_ts=now)
    assert not is_stale(very_old, "manual", now_ts=now)


# ---- resolve_chain ----

def test_resolve_chain_uses_static_when_manual_and_no_cache(tmp_path):
    """With policy=manual and no cache, must NOT touch network — return static."""
    chain = resolve_chain(
        policy="manual",
        cache_path=str(tmp_path / "no.json"),
        static_fallback=["a:free", "b"],
    )
    assert chain == ["a:free", "b"]


def test_resolve_chain_uses_cache_when_fresh(tmp_path):
    p = tmp_path / "cache.json"
    e = CacheEntry(
        refreshed_ts=int(time.time()) - 3600,  # 1 hour ago
        slugs=["cached:free", "cached-paid"],
        raw_count=1,
    )
    save_cache(str(p), e)
    chain = resolve_chain(
        policy="weekly",
        cache_path=str(p),
        static_fallback=["should-not-be-used"],
    )
    assert chain == ["cached:free", "cached-paid"]


def test_resolve_chain_uses_cache_with_never_policy(tmp_path):
    p = tmp_path / "cache.json"
    e = CacheEntry(
        refreshed_ts=1,  # ancient
        slugs=["very-old:free"],
        raw_count=1,
    )
    save_cache(str(p), e)
    chain = resolve_chain(
        policy="never",
        cache_path=str(p),
        static_fallback=[],
    )
    assert chain == ["very-old:free"]


def test_resolve_chain_minimal_fallback_when_nothing(tmp_path):
    """No cache, manual policy, no static — emit minimal chain."""
    chain = resolve_chain(
        policy="manual",
        cache_path=str(tmp_path / "no.json"),
        static_fallback=[],
    )
    assert PAID_FLOOR in chain
    assert any(s.endswith(":free") for s in chain)
