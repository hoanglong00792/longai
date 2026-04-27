"""Symbol → price lookup via CoinGecko's simple/price endpoint.

This module exists because the on_chain_ta MCP's `coingecko_token_info`
takes (chain, address), not a ticker. For symbol-based lookups (the
"what's ETH trading at" path and the /price slash command), we go
directly to CoinGecko's id-based endpoint with a hardcoded
symbol→coin-id map for the curated ASSET_MAP.

Direct HTTP, not MCP — this is data fetching, not chat completion, so
it doesn't intersect I3 (the LLM chokepoint) or I4 (budget caps). Free
endpoint; no API key needed.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Symbol → CoinGecko coin id. Covers ASSET_MAP from router.py. Keep in
# sync when ASSET_MAP grows. Symbols not listed here just don't get
# enriched — the LLM falls back to its tool-using path.
COINGECKO_IDS: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "AVAX": "avalanche-2",
    "MATIC": "matic-network",
    "DOT": "polkadot",
    "TRX": "tron",
    "TON": "the-open-network",
    "NEAR": "near",
    "ATOM": "cosmos",
    "FTM": "fantom",
    "ALGO": "algorand",
    "ICP": "internet-computer",
    "FIL": "filecoin",
    "APT": "aptos",
    "SUI": "sui",
    "LINK": "chainlink",
    "UNI": "uniswap",
    "AAVE": "aave",
    "MKR": "maker",
    "CRV": "curve-dao-token",
    "SUSHI": "sushi",
    "COMP": "compound-governance-token",
    "SNX": "havven",
    "LDO": "lido-dao",
    "GMX": "gmx",
    "USDT": "tether",
    "USDC": "usd-coin",
    "DAI": "dai",
}


_API = "https://api.coingecko.com/api/v3/simple/price"
_TIMEOUT_S = 10.0


async def coingecko_simple_price(
    symbol: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> dict | None:
    """Return ``{"symbol", "coin_id", "price_usd", "change_24h_pct"}`` or None.

    None means: unknown symbol, network failure, or empty response. Caller
    treats None as "no enrichment available" and proceeds without it.
    """
    coin_id = COINGECKO_IDS.get(symbol.upper())
    if not coin_id:
        return None
    params = {
        "ids": coin_id,
        "vs_currencies": "usd",
        "include_24hr_change": "true",
    }
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as c:
                r = await c.get(_API, params=params)
        else:
            r = await client.get(_API, params=params, timeout=_TIMEOUT_S)
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.debug("coingecko simple/price failed for %s: %s", symbol, exc)
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    info = data.get(coin_id) or {}
    price = info.get("usd")
    if price is None:
        return None
    change = info.get("usd_24h_change")
    return {
        "symbol": symbol.upper(),
        "coin_id": coin_id,
        "price_usd": float(price),
        "change_24h_pct": float(change) if change is not None else None,
    }


def format_price_line(data: dict) -> str:
    """Render a one-line ``ETH: $2,275.65 (+2.10% 24h)`` from the dict above."""
    line = f"{data['symbol']}: ${data['price_usd']:,.2f}"
    change = data.get("change_24h_pct")
    if change is not None:
        sign = "+" if change >= 0 else ""
        line += f" ({sign}{change:.2f}% 24h)"
    return line
