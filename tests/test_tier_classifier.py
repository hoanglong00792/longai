"""Tier classifier — regex-driven, no network."""
import pytest

from longai.tier_classifier import classify


@pytest.mark.parametrize("msg,tier", [
    # L: explicit research vocabulary
    ("research the latest on Solana validators", "L"),
    ("Can you do a deep dive on EigenLayer", "L"),
    ("comprehensive analysis of stablecoin issuers", "L"),
    ("find me 5 articles about ZK rollups", "L"),
    ("compare Aave with Compound", "L"),
    ("analyze BTC price action and the macro backdrop", "L"),

    # S: short greetings + tiny calc
    ("hi", "S"),
    ("hello there", "S"),
    ("thanks", "S"),
    ("good morning", "S"),
    ("calc 12 * 7", "S"),
    ("2+2", "S"),
    ("what's the time", "S"),

    # M: default for anything not obviously S or L
    ("can you tell me what GMX does", "M"),
    ("get the price of ETH", "M"),
    ("show me my wallet balance", "M"),
    ("write a tweet about onchain analytics", "M"),
])
def test_classify_keyword_routing(msg, tier):
    out_tier, _ = classify(msg)
    assert out_tier == tier, f"{msg!r} → got {out_tier}, want {tier}"


def test_quick_prefix_overrides_to_s():
    tier, cleaned = classify("/quick what is GMX")
    assert tier == "S"
    assert cleaned == "what is GMX"


def test_deep_prefix_overrides_to_l():
    tier, cleaned = classify("/deep tell me about pendle")
    assert tier == "L"
    assert cleaned == "tell me about pendle"


def test_prefix_stripping_preserves_inner_content():
    """The LLM should see the message without the routing prefix noise."""
    _, cleaned = classify("/deep analyze the Aave-Compound merger thesis")
    assert cleaned.startswith("analyze")
    assert "/deep" not in cleaned


def test_empty_message_defaults_to_m():
    assert classify("") == ("M", "")
    assert classify("   ") == ("M", "")


def test_long_message_with_quick_keyword_is_not_s():
    """Length cap protects against false-positive S routing on long requests."""
    msg = "calc me a table of revenue and growth across all the chains we cover"
    tier, _ = classify(msg)
    assert tier == "M"  # too long to be S despite "calc"
