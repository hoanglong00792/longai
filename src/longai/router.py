"""Deterministic intent router — 0ms regex/lookup, no LLM.

Inspired by ../bot-trading-signals/core/router.py. Cuts the "5-turn ETH price"
problem at the source: detect that a message references a known token
*before* the LLM runs, so enrichment can fetch the data upfront and the LLM
answers in one turn instead of iteratively calling tools.

V1 is intentionally narrow — only crypto majors, English-only finance hints,
no LLM verify, no contract detection. Expand iteratively as false-positives
or false-negatives surface.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# Curated set of high-confidence symbols. Anything here, when it appears
# alone or with a finance hint, is treated as a real ticker reference.
# Adding to this list is fine; pruning requires a regression check.
ASSET_MAP: frozenset[str] = frozenset({
    # Layer-1 / majors
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "MATIC", "DOT",
    "TRX", "TON", "NEAR", "ATOM", "FTM", "ALGO", "ICP", "FIL", "APT", "SUI",
    # DeFi / utility
    "LINK", "UNI", "AAVE", "MKR", "CRV", "SUSHI", "COMP", "SNX", "LDO", "GMX",
    # Stables (rarely the subject but worth recognizing)
    "USDT", "USDC", "DAI",
})

# Spelled-out names → ticker. Lowercase keys, uppercase values.
ALIASES: dict[str, str] = {
    "bitcoin": "BTC",
    "ether": "ETH",
    "ethereum": "ETH",
    "solana": "SOL",
    "binance": "BNB",
    "ripple": "XRP",
    "cardano": "ADA",
    "dogecoin": "DOGE",
    "polygon": "MATIC",
    "avalanche": "AVAX",
    "polkadot": "DOT",
    "chainlink": "LINK",
    "uniswap": "UNI",
}

# English finance hints. Word-boundary matched. Vietnamese support deferred.
_FINANCE_HINTS: frozenset[str] = frozenset({
    "price", "trading", "trade", "trades", "buy", "sell", "selling", "buying",
    "bullish", "bearish", "pump", "dump", "rally", "crash", "moon", "dip",
    "rsi", "ta", "chart", "analyze", "analysis", "support", "resistance",
    "long", "short", "leverage", "margin", "liquidation",
    "breakout", "reversal", "consolidation", "uptrend", "downtrend",
    "value", "worth", "cost", "rate",
})

# Common short words that match ticker patterns but aren't tickers.
# Belt-and-suspenders against ASSET_MAP false positives.
_COMMON_WORDS: frozenset[str] = frozenset({
    "A", "I", "AM", "PM", "OK", "NO", "GO", "DO", "ME", "IT", "TO", "OF",
    "ETC", "USA", "EU", "USD", "EUR", "GBP", "JPY",
})

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


@dataclass
class RouteHints:
    """Hints emitted by ``classify``. Consumed by ``enrichment.enrich``."""
    symbol: str | None = None
    has_finance_hint: bool = False
    # Future fields (PR E+): contract, chain, search_query, scan, vision_url


def _scan_symbols(text: str) -> str | None:
    """Find the first plausible ticker in *text* (or None).

    Order: alias map (full-word) → ASSET_MAP (uppercase token). Returns
    uppercase symbol.
    """
    lower = text.lower()
    # Multi-word aliases would go here; v1 has only single-word aliases.
    for word in _WORD_RE.findall(lower):
        if word in ALIASES:
            return ALIASES[word]
    for word in _WORD_RE.findall(text):
        upper = word.upper()
        if upper in ASSET_MAP and upper not in _COMMON_WORDS:
            return upper
    return None


def _has_finance_hint(text: str) -> bool:
    lower = text.lower()
    words = set(_WORD_RE.findall(lower))
    return bool(words & _FINANCE_HINTS)


def classify(text: str) -> RouteHints:
    """Deterministic intent classification.

    A symbol is reported only when it's high-confidence:
      - the curated alias map matches (e.g. "ethereum"), OR
      - the symbol appears AND a finance hint is present (e.g. "BTC price")

    A bare three-letter ticker like "BTC" alone is also returned, but a
    sentence like "BTC said hi" with no finance hint stays unhinted (the
    LLM can decide what to do).
    """
    if not text or not text.strip():
        return RouteHints()
    symbol = _scan_symbols(text)
    has_hint = _has_finance_hint(text)
    if symbol is None:
        return RouteHints(has_finance_hint=has_hint)
    # If we matched via ALIASES, finance hint not required — "ethereum" is
    # already a strong signal. If we matched via ASSET_MAP, require a hint
    # OR the message to be short/symbolic (e.g. just "BTC").
    matched_via_alias = any(
        a in text.lower() and ALIASES[a] == symbol for a in ALIASES
    )
    short_message = len(text.split()) <= 3
    if matched_via_alias or has_hint or short_message:
        return RouteHints(symbol=symbol, has_finance_hint=has_hint)
    return RouteHints(has_finance_hint=has_hint)
