"""Tests for the auto-classifier used by refresh_models.sh --tiered."""
import textwrap

import pytest

from longai.tier_auto_classifier import (
    auto_tier_from_slug,
    classify_models,
    diff_chains,
    is_free,
    load_overrides,
)


# ── auto_tier_from_slug ──────────────────────────────────────────────────


@pytest.mark.parametrize("slug,expected", [
    ("nvidia/nemotron-nano-9b-v2:free", "S"),
    ("qwen/qwen3-coder:free", "S"),  # no param hint → fallback to S (no ctx given)
    ("google/gemma-4-26b-a4b-it:free", "S"),  # 26B < 30 threshold
    ("google/gemma-4-31b-it:free", "M"),
    ("meta-llama/llama-3.3-70b-instruct:free", "M"),
    ("nvidia/nemotron-3-super-120b-a12b:free", "L"),
    ("openai/gpt-oss-120b:free", "L"),
    ("inclusionai/ling-2.6-1t:free", "L"),
])
def test_param_hint_routing(slug, expected):
    assert auto_tier_from_slug(slug) == expected


def test_ctx_length_fallback_when_no_param_hint():
    # No "Nb" or "Nt" in slug → use context length
    assert auto_tier_from_slug("openrouter/free", ctx_len=200_000) == "L"
    assert auto_tier_from_slug("openrouter/free", ctx_len=64_000) == "M"
    assert auto_tier_from_slug("openrouter/free", ctx_len=8_000) == "S"


def test_decimal_billions_handled():
    assert auto_tier_from_slug("liquid/lfm-2.5-1.2b-instruct:free") == "S"


# ── is_free ──────────────────────────────────────────────────────────────


def test_is_free_recognizes_suffix():
    assert is_free("google/gemma-4-26b-a4b-it:free")


def test_is_free_recognizes_exception():
    assert is_free("openrouter/free")


def test_is_free_rejects_paid():
    assert not is_free("google/gemma-4-26b-a4b-it")


# ── load_overrides ───────────────────────────────────────────────────────


def test_load_overrides_missing_file_returns_empty(tmp_path):
    overrides, skip = load_overrides(str(tmp_path / "missing.toml"))
    assert overrides == {}
    assert skip == set()


def test_load_overrides_parses_valid_file(tmp_path):
    p = tmp_path / "tiers.toml"
    p.write_text(textwrap.dedent("""
        [overrides]
        "qwen/qwen3-coder:free" = "L"
        "google/gemma-4-26b-a4b-it:free" = "M"
        "openrouter/free" = "s"

        [skip]
        slugs = ["baidu/qianfan-ocr-fast:free"]
    """))
    overrides, skip = load_overrides(str(p))
    assert overrides == {
        "qwen/qwen3-coder:free": "L",
        "google/gemma-4-26b-a4b-it:free": "M",
        "openrouter/free": "S",  # normalized to upper
    }
    assert skip == {"baidu/qianfan-ocr-fast:free"}


def test_load_overrides_drops_invalid_tier_values(tmp_path):
    p = tmp_path / "tiers.toml"
    p.write_text(textwrap.dedent("""
        [overrides]
        "x/y:free" = "Z"
        "a/b:free" = "M"
    """))
    overrides, _ = load_overrides(str(p))
    assert overrides == {"a/b:free": "M"}


def test_load_overrides_handles_malformed_toml(tmp_path):
    p = tmp_path / "tiers.toml"
    p.write_text("[unclosed")
    overrides, skip = load_overrides(str(p))
    assert overrides == {}
    assert skip == set()


# ── classify_models ──────────────────────────────────────────────────────


def test_classify_paid_goes_to_fallback(tmp_path):
    out = classify_models(
        [("google/gemma-4-26b-a4b-it", 32_000)],
        override_path=str(tmp_path / "missing.toml"),
    )
    assert out.tier_chains["fallback"] == ["google/gemma-4-26b-a4b-it"]
    assert out.tier_chains["S"] == []


def test_classify_free_uses_param_hint(tmp_path):
    models = [
        ("nvidia/nemotron-nano-9b-v2:free", 8000),
        ("google/gemma-4-31b-it:free", 32000),
        ("inclusionai/ling-2.6-1t:free", 64000),
    ]
    out = classify_models(models, override_path=str(tmp_path / "missing.toml"))
    assert out.tier_chains["S"] == ["nvidia/nemotron-nano-9b-v2:free"]
    assert out.tier_chains["M"] == ["google/gemma-4-31b-it:free"]
    assert out.tier_chains["L"] == ["inclusionai/ling-2.6-1t:free"]


def test_classify_skip_drops_entirely(tmp_path):
    p = tmp_path / "tiers.toml"
    p.write_text('[skip]\nslugs = ["bad/model:free"]\n')
    out = classify_models(
        [("bad/model:free", 0), ("good/9b:free", 0)],
        override_path=str(p),
    )
    assert "bad/model:free" not in {s for slugs in out.tier_chains.values() for s in slugs}
    assert out.skipped == ["bad/model:free"]
    assert out.tier_chains["S"] == ["good/9b:free"]


def test_classify_override_wins_over_auto(tmp_path):
    p = tmp_path / "tiers.toml"
    p.write_text(textwrap.dedent("""
        [overrides]
        "nvidia/nemotron-nano-9b-v2:free" = "L"
    """))
    out = classify_models(
        [("nvidia/nemotron-nano-9b-v2:free", 0)],
        override_path=str(p),
    )
    assert out.tier_chains["L"] == ["nvidia/nemotron-nano-9b-v2:free"]
    assert out.tier_chains["S"] == []
    assert out.overrides_applied == {"nvidia/nemotron-nano-9b-v2:free": "L"}


def test_classify_new_slugs_flagged_for_review(tmp_path):
    previous = {"S": ["a/9b:free"], "M": [], "L": [], "fallback": []}
    out = classify_models(
        [
            ("a/9b:free", 0),         # known
            ("b/12b:free", 0),        # NEW (auto → S)
            ("c/120b:free", 0),       # NEW (auto → L)
        ],
        override_path=str(tmp_path / "missing.toml"),
        previous_chains=previous,
    )
    new_slugs = {s for s, _ in out.auto_classified_new}
    assert new_slugs == {"b/12b:free", "c/120b:free"}


# ── diff_chains ──────────────────────────────────────────────────────────


def test_diff_no_previous_returns_summary():
    cur = {"S": ["a"], "M": [], "L": [], "fallback": []}
    out = diff_chains(None, cur)
    assert "1 slugs newly classified" in out[0]


def test_diff_detects_added_removed_moved():
    prev = {"S": ["a"], "M": ["b"], "L": ["c"], "fallback": []}
    cur = {"S": ["a"], "M": [], "L": ["c", "b"], "fallback": ["d"]}
    out = diff_chains(prev, cur)
    text = "\n".join(out)
    assert "+ fallback" in text and "d" in text
    assert "M -> L" in text and "b" in text
    assert "(no changes)" not in text


def test_diff_unchanged_returns_no_changes_line():
    prev = {"S": ["a"], "M": [], "L": [], "fallback": []}
    cur = {"S": ["a"], "M": [], "L": [], "fallback": []}
    assert diff_chains(prev, cur) == ["  (no changes)"]
