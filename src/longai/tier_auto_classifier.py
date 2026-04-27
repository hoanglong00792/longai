"""Auto-classify OpenRouter slugs into tiers S/M/L (+ fallback).

Used by ``scripts/refresh_models.sh --tiered`` to convert a flat list of
free + tool-capable models into the four-chain shape that
``BudgetGuard.chat(tier=...)`` expects.

Strategy:
  1. Skip list — drop entirely (broken tools support, OCR-only, etc.)
  2. Manual overrides — pin specific slugs to specific tiers
  3. Paid (non-:free) models → ``fallback`` chain (unless overridden)
  4. Auto-classify free models by parameter count parsed from slug,
     with context-length as a fallback signal

Override file: ``~/.longai/model_tiers.local.toml`` (user-maintained).
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# "70b", "120B", "1.2t" — parameter hint embedded in the slug
_PARAM_RX = re.compile(r"(\d+(?:\.\d+)?)\s*(b|t)\b", re.IGNORECASE)

# Free slugs that don't follow the `:free` suffix convention. Mirrors
# budget_guard._FREE_EXCEPTIONS — kept in sync by hand for now.
_FREE_EXCEPTIONS = frozenset({"openrouter/free"})

DEFAULT_OVERRIDE_PATH = "~/.longai/model_tiers.local.toml"

VALID_TIERS = ("S", "M", "L", "FALLBACK")


@dataclass
class TierAssignment:
    """Result of classifying a list of (slug, ctx_len) pairs."""
    tier_chains: dict[str, list[str]] = field(
        default_factory=lambda: {"S": [], "M": [], "L": [], "fallback": []}
    )
    overrides_applied: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    # Slugs that weren't in the previous classification — surfaced so the
    # user can review and add to overrides if the auto-pick is wrong.
    auto_classified_new: list[tuple[str, str]] = field(default_factory=list)


def is_free(slug: str) -> bool:
    return slug.endswith(":free") or slug in _FREE_EXCEPTIONS


def auto_tier_from_slug(slug: str, ctx_len: int = 0) -> str:
    """Pick S | M | L from slug parameter hint, or context length fallback.

    Returns "S" for slugs without any signal at all (conservative — small
    models do less damage on a budget-bound bot).
    """
    m = _PARAM_RX.search(slug.lower())
    if m:
        b = float(m.group(1)) * (1000 if m.group(2).lower() == "t" else 1)
        if b < 30:
            return "S"
        if b < 80:
            return "M"
        return "L"
    if ctx_len >= 128_000:
        return "L"
    if ctx_len >= 32_000:
        return "M"
    return "S"


def load_overrides(
    path: str = DEFAULT_OVERRIDE_PATH,
) -> tuple[dict[str, str], set[str]]:
    """Return (overrides_map, skip_set) from the user override file.

    Empty if the file is missing — overrides are optional.
    Override values are normalized to upper-case ("S" / "M" / "L" / "FALLBACK").
    """
    p = Path(os.path.expanduser(path))
    if not p.exists():
        return ({}, set())
    try:
        raw = tomllib.loads(p.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        # Fail soft — bad override file shouldn't kill a refresh.
        return ({}, set())
    overrides_raw = raw.get("overrides") or {}
    overrides: dict[str, str] = {}
    for slug, tier in overrides_raw.items():
        t = str(tier).upper().strip()
        if t in VALID_TIERS:
            overrides[slug] = t
    skip_block = raw.get("skip") or {}
    skip_list = set(skip_block.get("slugs") or [])
    return (overrides, skip_list)


def classify_models(
    models: Iterable[tuple[str, int]],
    *,
    paid_floor_to_fallback: bool = True,
    override_path: str = DEFAULT_OVERRIDE_PATH,
    previous_chains: dict[str, list[str]] | None = None,
) -> TierAssignment:
    """Classify (slug, ctx_len) pairs into tier chains.

    Args:
        models: iterable of (slug, ctx_len) pairs from OpenRouter listing
        paid_floor_to_fallback: when True, non-free slugs default to the
            fallback chain instead of being auto-classified
        override_path: path to user override TOML (skip + overrides)
        previous_chains: prior tier_chains output, used only to decide
            which slugs are "new" for the action-items report

    Returns:
        TierAssignment with chains, applied overrides, skipped slugs, and
        the list of newly-auto-classified slugs.
    """
    overrides, skip_set = load_overrides(override_path)
    out = TierAssignment()

    prev_known: set[str] = set()
    if previous_chains:
        for slugs in previous_chains.values():
            prev_known.update(slugs)

    for slug, ctx in models:
        if slug in skip_set:
            out.skipped.append(slug)
            continue
        if slug in overrides:
            tier = overrides[slug]
            key = "fallback" if tier == "FALLBACK" else tier
            out.tier_chains[key].append(slug)
            out.overrides_applied[slug] = tier
            continue
        if paid_floor_to_fallback and not is_free(slug):
            out.tier_chains["fallback"].append(slug)
            continue
        tier = auto_tier_from_slug(slug, ctx)
        out.tier_chains[tier].append(slug)
        if slug not in prev_known:
            out.auto_classified_new.append((slug, tier))

    return out


def diff_chains(
    previous: dict[str, list[str]] | None,
    current: dict[str, list[str]],
) -> list[str]:
    """Produce a human-readable diff between two chain mappings.

    Returns a list of lines (no trailing newlines).
    """
    if not previous:
        return [f"  (no previous chains — {sum(len(v) for v in current.values())} slugs newly classified)"]
    lines: list[str] = []
    # Build slug → tier maps
    prev_tier: dict[str, str] = {}
    for tier, slugs in previous.items():
        for s in slugs:
            prev_tier[s] = tier
    cur_tier: dict[str, str] = {}
    for tier, slugs in current.items():
        for s in slugs:
            cur_tier[s] = tier

    added = set(cur_tier) - set(prev_tier)
    removed = set(prev_tier) - set(cur_tier)
    moved = {
        s: (prev_tier[s], cur_tier[s])
        for s in (set(prev_tier) & set(cur_tier))
        if prev_tier[s] != cur_tier[s]
    }
    for s in sorted(added):
        lines.append(f"  + {cur_tier[s]:<8} {s}  (new)")
    for s in sorted(removed):
        lines.append(f"  - {prev_tier[s]:<8} {s}  (delisted)")
    for s in sorted(moved):
        old, new = moved[s]
        lines.append(f"  ~ {old} -> {new}  {s}")
    if not lines:
        lines.append("  (no changes)")
    return lines
