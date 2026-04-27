"""Deterministic intent router — symbol scanner, finance hints."""
import pytest

from longai.router import RouteHints, classify


# ── Alias matches (high-confidence; no finance hint required) ──────────


@pytest.mark.parametrize("msg,sym", [
    ("what's ethereum doing today", "ETH"),
    ("bitcoin to the moon", "BTC"),
    ("solana ecosystem", "SOL"),
])
def test_aliases_match(msg, sym):
    h = classify(msg)
    assert h.symbol == sym


# ── Symbol + finance hint ──────────────────────────────────────────────


@pytest.mark.parametrize("msg,sym", [
    ("what's ETH trading at", "ETH"),
    ("BTC price now", "BTC"),
    ("can I buy SOL safely", "SOL"),
    ("LINK is bullish", "LINK"),
])
def test_symbol_with_hint(msg, sym):
    h = classify(msg)
    assert h.symbol == sym
    assert h.has_finance_hint


# ── Bare short messages ────────────────────────────────────────────────


def test_bare_ticker_short_message():
    """Just 'BTC' is enough; short messages don't need a hint."""
    h = classify("BTC")
    assert h.symbol == "BTC"


def test_two_word_ticker_query():
    """'ETH price' — short, with hint."""
    h = classify("ETH price")
    assert h.symbol == "ETH"
    assert h.has_finance_hint


# ── False positives we must avoid ──────────────────────────────────────


def test_etc_does_not_match_etc_token():
    """'etc' is in COMMON_WORDS — never a ticker."""
    h = classify("get me coffee, sandwich, etc")
    assert h.symbol is None


def test_long_message_with_ticker_but_no_hint():
    """'BTC said hi to me yesterday at the conference' — long, no hint, ASSET_MAP-only.
    Not a finance query; symbol should not be returned."""
    h = classify("BTC said hi to me yesterday at the conference and we talked")
    assert h.symbol is None


def test_empty_message():
    assert classify("") == RouteHints()
    assert classify("   ") == RouteHints()


# ── Finance hint without symbol ────────────────────────────────────────


def test_finance_hint_no_symbol():
    h = classify("what's the bullish breakout look like")
    assert h.symbol is None
    assert h.has_finance_hint


# ── No finance, no symbol ──────────────────────────────────────────────


def test_pure_chat():
    h = classify("how was your day")
    assert h.symbol is None
    assert not h.has_finance_hint
