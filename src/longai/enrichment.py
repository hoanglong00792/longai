"""Pre-LLM data enrichment — fetch context upfront so the LLM answers in one turn.

Pattern from ../bot-trading-signals/enrichment_flow.md: detect what the
message references (symbol, contract, etc.) via the deterministic router,
fetch the data via direct MCP calls, and inject it into the user message
as a context block. The LLM then has the data already and shouldn't need
to call tools — saving 4+ wasted turns on free models that loop on
verification.

Attention asymmetry: context goes BEFORE the question (lower attention),
question stays last (highest attention). The LLM weights its response
toward what's at the end.
"""
from __future__ import annotations

import json
import logging

from longai.mcp_client import MCPRegistry, UnknownTool
from longai.router import RouteHints

logger = logging.getLogger(__name__)


async def _fetch_market(symbol: str, mcp: MCPRegistry) -> str | None:
    """Direct CoinGecko fetch via the on_chain_ta MCP. Returns a one-line
    formatted block, or None if unavailable / errored.
    """
    try:
        raw = await mcp.call("coingecko_token_info", {"symbol": symbol})
    except UnknownTool:
        logger.debug("coingecko_token_info MCP not available; skipping enrichment")
        return None
    except Exception as exc:
        logger.debug("coingecko_token_info call failed: %s", exc)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if "error" in data:
        return None
    price = (
        data.get("current_price_usd")
        or data.get("current_price")
        or (data.get("market_data") or {}).get("current_price", {}).get("usd")
    )
    change = (
        data.get("price_change_24h_pct")
        or data.get("change_pct")
        or (data.get("market_data") or {}).get("price_change_percentage_24h")
    )
    if price is None:
        return None
    line = f"{symbol}: ${float(price):,.2f}"
    if change is not None:
        try:
            sign = "+" if float(change) >= 0 else ""
            line += f" ({sign}{float(change):.2f}% 24h)"
        except (TypeError, ValueError):
            pass
    return line


async def enrich(hints: RouteHints, mcp: MCPRegistry) -> str | None:
    """Build a context block for the user message, or None if nothing to add.

    The block includes a directive telling the LLM not to re-fetch — free
    models often spend turns "verifying" data that's already in context.
    """
    blocks: list[str] = []
    if hints.symbol:
        market = await _fetch_market(hints.symbol, mcp)
        if market:
            blocks.append(market)
    if not blocks:
        return None
    body = "\n".join(blocks)
    return (
        "[Market context — pre-fetched, current. Answer from this data; "
        "do not call tools again unless the user asks for something not "
        "covered here.]\n"
        f"{body}"
    )


def attach(user_message: str, context_block: str | None) -> str:
    """Prepend *context_block* to *user_message* (or return as-is)."""
    if not context_block:
        return user_message
    return f"{context_block}\n\n{user_message}"
