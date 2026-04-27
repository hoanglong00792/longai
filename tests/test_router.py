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


# ── PR E: contract + URL detection ─────────────────────────────────────


def test_evm_contract_detected_default_ethereum():
    h = classify("look at 0x6982508145454ce325ddbe47a25d4ec3d2311933 for me")
    assert h.contract == "0x6982508145454ce325ddbe47a25d4ec3d2311933"
    assert h.chain == "ethereum"


def test_evm_contract_with_chain_hint_base():
    h = classify("0x4200000000000000000000000000000000000006 on base")
    assert h.contract is not None
    assert h.chain == "base"


def test_evm_contract_with_chain_hint_arbitrum():
    h = classify("0x912ce59144191c1204e64559fe8253a0e49e6548 on arbitrum")
    assert h.chain == "arbitrum"


def test_no_contract_when_no_address():
    h = classify("just a chat about ETH")
    assert h.contract is None


def test_url_detected():
    h = classify("read https://example.com/article please")
    assert h.urls == ["https://example.com/article"]


def test_url_strips_trailing_punctuation():
    h = classify("see https://example.com/article. it's wild")
    assert h.urls == ["https://example.com/article"]


def test_multiple_urls_deduplicated():
    h = classify("see https://a.com and https://b.com and https://a.com again")
    assert h.urls == ["https://a.com", "https://b.com"]


def test_no_urls_in_plain_chat():
    h = classify("just chatting")
    assert h.urls == []


def test_combined_contract_url_symbol():
    """All three detection signals can fire on one message."""
    h = classify(
        "what about ETH price for 0x6982508145454ce325ddbe47a25d4ec3d2311933 "
        "see https://etherscan.io"
    )
    assert h.symbol == "ETH"
    assert h.contract == "0x6982508145454ce325ddbe47a25d4ec3d2311933"
    assert h.urls == ["https://etherscan.io"]
    assert h.has_finance_hint  # "price"
