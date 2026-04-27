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
from dataclasses import dataclass, field


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

# EVM contract / wallet address — 0x + 40 hex. Word boundary so addresses
# embedded in URLs or longer hex strings don't match.
_EVM_ADDR_RE = re.compile(r"\b(0x[a-fA-F0-9]{40})\b")

# HTTP(S) URLs. Conservative — accept up to whitespace or a few terminating
# punctuation chars. Not RFC-perfect; doesn't need to be.
_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+")

# Chain hint detection (mirrors bts router; lighter set for v1).
_CHAIN_HINTS: dict[str, str] = {
    "ethereum": "ethereum", "mainnet": "ethereum", "eth": "ethereum",
    "base": "base",
    "arbitrum": "arbitrum", "arb": "arbitrum",
    "optimism": "optimism", "op": "optimism",
    "polygon": "polygon", "matic": "polygon",
    "bsc": "bsc", "binance smart chain": "bsc", "bnb chain": "bsc",
    "avalanche": "avalanche", "avax": "avalanche",
}


@dataclass
class RouteHints:
    """Hints emitted by ``classify``. Consumed by ``enrichment.enrich``."""
    symbol: str | None = None
    has_finance_hint: bool = False
    # PR E lean — contract & URL detection
    contract: str | None = None
    chain: str | None = None  # only meaningful when contract is set
    urls: list[str] = field(default_factory=list)
    # Future fields: search_query, vision_url, scan


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


def _detect_chain(text: str) -> str | None:
    """Return the first chain hint in *text*, or None.

    Order matters — multi-word hints checked before single-word so
    "binance smart chain" wins over "binance" alone.
    """
    lower = text.lower()
    for hint, chain in sorted(_CHAIN_HINTS.items(), key=lambda kv: -len(kv[0])):
        if hint in lower:
            return chain
    return None


def _detect_contract(text: str) -> tuple[str | None, str | None]:
    """Return (address, chain) — chain inferred from text hints, defaults to ethereum."""
    m = _EVM_ADDR_RE.search(text)
    if not m:
        return (None, None)
    address = m.group(1)
    chain = _detect_chain(text) or "ethereum"
    return (address, chain)


def _extract_urls(text: str) -> list[str]:
    """Return URLs in *text* (deduplicated, order-preserving)."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def classify(text: str) -> RouteHints:
    """Deterministic intent classification.

    Detection order (more specific wins):
      1. EVM contract address  → contract + chain
      2. URLs                  → urls list
      3. Symbol                → symbol (alias-match always; ASSET_MAP only with hint or short message)
      4. Finance hints (bare)  → has_finance_hint flag

    All applicable hints are returned; multiple enrichments can run in parallel.
    """
    if not text or not text.strip():
        return RouteHints()

    contract, chain = _detect_contract(text)
    urls = _extract_urls(text)
    symbol = _scan_symbols(text)
    has_hint = _has_finance_hint(text)

    # Symbol gating — same rules as before, but factored out
    keep_symbol = False
    if symbol is not None:
        matched_via_alias = any(
            a in text.lower() and ALIASES[a] == symbol for a in ALIASES
        )
        short_message = len(text.split()) <= 3
        keep_symbol = matched_via_alias or has_hint or short_message

    return RouteHints(
        symbol=symbol if keep_symbol else None,
        has_finance_hint=has_hint,
        contract=contract,
        chain=chain,
        urls=urls,
    )
