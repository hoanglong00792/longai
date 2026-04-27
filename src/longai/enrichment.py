"""Pre-LLM data enrichment — fetch context upfront so the LLM answers in one turn.

Pattern from ../bot-trading-signals/enrichment_flow.md: detect what the
message references (symbol, contract, URL) via the deterministic router,
fetch the data via direct MCP calls, and inject it into the user message
as a context block. The LLM then has the data already and shouldn't need
to call tools — saving 4+ wasted turns on free models that loop on
verification.

Attention asymmetry: context goes BEFORE the question (lower attention),
question stays last (highest attention). The LLM weights its response
toward what's at the end.

PR E lean adds contract + URL enrichment alongside the original market
fetch. All applicable enrichments run in parallel via ``asyncio.gather``.
"""
from __future__ import annotations

import asyncio
import json
import logging

from longai.mcp_client import MCPRegistry, UnknownTool
from longai.router import RouteHints

logger = logging.getLogger(__name__)

# Truncation cap for fetched URL bodies — protects context window. Free
# models often have 32k context; 1.5k tokens of one article is plenty.
_URL_BODY_CAP = 1500
# Hard cap on URLs we'll fetch per message — defensive against link-spam.
_MAX_URLS = 3


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


async def _fetch_contract(
    address: str, chain: str | None, mcp: MCPRegistry,
) -> str | None:
    """Direct DexScreener fetch via the on_chain_ta MCP. One-line summary."""
    args: dict = {"address": address}
    if chain:
        args["chain"] = chain
    try:
        raw = await mcp.call("dexscreener_pairs", args)
    except UnknownTool:
        logger.debug("dexscreener_pairs MCP not available; skipping contract enrichment")
        return None
    except Exception as exc:
        logger.debug("dexscreener_pairs call failed: %s", exc)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if "error" in data:
        return None
    # DexScreener returns a list of pairs; pick the most-liquid.
    pairs = data.get("pairs") or []
    if not pairs:
        return None
    pair = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
    base = (pair.get("baseToken") or {}).get("symbol") or "?"
    name = (pair.get("baseToken") or {}).get("name") or ""
    price = pair.get("priceUsd")
    liq = (pair.get("liquidity") or {}).get("usd")
    vol = (pair.get("volume") or {}).get("h24")
    parts = [f"{base}"]
    if name and name != base:
        parts.append(f"({name})")
    parts.append(f"{address[:6]}…{address[-4:]}")
    if chain:
        parts.append(f"on {chain}")
    head = " ".join(parts)
    metrics: list[str] = []
    if price:
        try:
            metrics.append(f"${float(price):,.6f}".rstrip("0").rstrip("."))
        except (TypeError, ValueError):
            pass
    if liq:
        try:
            metrics.append(f"liq ${float(liq):,.0f}")
        except (TypeError, ValueError):
            pass
    if vol:
        try:
            metrics.append(f"vol24h ${float(vol):,.0f}")
        except (TypeError, ValueError):
            pass
    if not metrics:
        return head
    return f"{head} — {', '.join(metrics)}"


async def _fetch_url(url: str, mcp: MCPRegistry) -> str | None:
    """Direct fetch via the url_fetch MCP. Returns a truncated extract or None."""
    try:
        raw = await mcp.call("fetch_url", {"url": url})
    except UnknownTool:
        logger.debug("fetch_url MCP not available; skipping URL enrichment")
        return None
    except Exception as exc:
        logger.debug("fetch_url call failed for %s: %s", url, exc)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # MCP returned a plain string (some tools do); use as-is
        body = raw
    else:
        if "error" in data:
            return None
        body = data.get("text") or data.get("content") or data.get("body") or ""
    if not body:
        return None
    body = body.strip()
    if len(body) > _URL_BODY_CAP:
        body = body[:_URL_BODY_CAP].rstrip() + "  […truncated]"
    return f"[{url}]\n{body}"


async def enrich(hints: RouteHints, mcp: MCPRegistry) -> str | None:
    """Build a context block for the user message, or None if nothing to add.

    Runs all applicable enrichments concurrently. The block includes a
    directive telling the LLM not to re-fetch — free models often spend
    turns "verifying" data that's already in context.
    """
    tasks: list = []
    if hints.symbol:
        tasks.append(("market", _fetch_market(hints.symbol, mcp)))
    if hints.contract:
        tasks.append(("contract", _fetch_contract(hints.contract, hints.chain, mcp)))
    for url in hints.urls[:_MAX_URLS]:
        tasks.append(("url", _fetch_url(url, mcp)))

    if not tasks:
        return None

    results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)

    blocks: list[str] = []
    for (kind, _), result in zip(tasks, results):
        if isinstance(result, Exception):
            logger.debug("enrichment %s raised: %s", kind, result)
            continue
        if result:
            blocks.append(result)

    if not blocks:
        return None

    body = "\n".join(blocks)
    return (
        "[Pre-fetched context — current. Answer from this data; do not call "
        "tools again unless the user asks for something not covered here.]\n"
        f"{body}"
    )


def attach(user_message: str, context_block: str | None) -> str:
    """Prepend *context_block* to *user_message* (or return as-is)."""
    if not context_block:
        return user_message
    return f"{context_block}\n\n{user_message}"
