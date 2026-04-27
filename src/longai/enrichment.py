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
import time
from typing import TYPE_CHECKING

from longai.mcp_client import MCPRegistry, UnknownTool
from longai.prices import coingecko_simple_price, format_price_line
from longai.router import RouteHints

if TYPE_CHECKING:
    from longai.trace import Tracer

logger = logging.getLogger(__name__)

# Truncation cap for fetched URL bodies — protects context window. Free
# models often have 32k context; 1.5k tokens of one article is plenty.
_URL_BODY_CAP = 1500
# Hard cap on URLs we'll fetch per message — defensive against link-spam.
_MAX_URLS = 3


async def _fetch_market(symbol: str, mcp: MCPRegistry) -> str | None:
    """Symbol-based price lookup via CoinGecko's simple/price endpoint.

    Note: the on_chain_ta MCP's `coingecko_token_info` is contract-based
    (requires chain+address), so we use the id-based simple/price API
    directly via the prices module. ``mcp`` parameter kept for signature
    parity with the other enrichment fetchers.
    """
    data = await coingecko_simple_price(symbol)
    if data is None:
        return None
    return format_price_line(data)


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


async def _timed(
    coro, *, kind: str, tracer: "Tracer | None", **extra,
) -> str | None:
    """Wrap an enrichment coroutine to record its wall-clock to the tracer."""
    t0 = time.perf_counter()
    try:
        result = await coro
    finally:
        if tracer is not None:
            ms = (time.perf_counter() - t0) * 1000.0
            tracer.timing(f"enrich.{kind}", ms, **extra)
    return result


async def enrich(
    hints: RouteHints,
    mcp: MCPRegistry,
    *,
    tracer: "Tracer | None" = None,
) -> str | None:
    """Build a context block for the user message, or None if nothing to add.

    Runs all applicable enrichments concurrently. The block includes a
    directive telling the LLM not to re-fetch — free models often spend
    turns "verifying" data that's already in context.
    """
    tasks: list = []
    if hints.symbol:
        tasks.append(("market", _timed(_fetch_market(hints.symbol, mcp),
                                       kind="market", tracer=tracer,
                                       symbol=hints.symbol)))
    if hints.contract:
        tasks.append(("contract", _timed(
            _fetch_contract(hints.contract, hints.chain, mcp),
            kind="contract", tracer=tracer,
            contract=hints.contract, chain=hints.chain,
        )))
    for url in hints.urls[:_MAX_URLS]:
        tasks.append(("url", _timed(_fetch_url(url, mcp),
                                    kind="url", tracer=tracer, url=url)))

    if not tasks:
        return None

    enrich_t0 = time.perf_counter()
    results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)
    enrich_ms = (time.perf_counter() - enrich_t0) * 1000.0
    if tracer is not None:
        tracer.timing("enrich.total", enrich_ms, kinds=[k for k, _ in tasks])

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
