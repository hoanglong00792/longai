"""Slash commands that bypass the agent loop entirely.

Pattern from ../bot-trading-signals/handlers/trading.py: for known patterns
(/price BTC, /ta ETH), skip the LLM. Direct data → direct format → direct
reply. No turns, no spend, no model variability.

Each handler returns a ``FastResult`` with text + optional error. The
caller wraps it in the standard envelope so traces/log_trace stay uniform.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from longai.mcp_client import MCPRegistry, UnknownTool
from longai.persistence import Persistence

logger = logging.getLogger(__name__)


@dataclass
class FastResult:
    text: str
    error: str | None = None


CommandHandler = Callable[[str, MCPRegistry, Persistence], Awaitable[FastResult]]


# ── Handlers ─────────────────────────────────────────────────────────────


async def cmd_price(arg: str, mcp: MCPRegistry, p: Persistence) -> FastResult:
    """``/price <symbol>`` — direct CoinGecko lookup."""
    symbol = arg.strip().upper()
    if not symbol:
        return FastResult(text="Usage: /price <symbol>\nExample: /price ETH")
    try:
        raw = await mcp.call("coingecko_token_info", {"symbol": symbol})
    except UnknownTool:
        return FastResult(
            text="Price lookup not available (coingecko_token_info MCP missing).",
            error="missing_tool",
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return FastResult(text="Could not parse price data.", error="bad_response")
    if "error" in data:
        return FastResult(
            text=f"Could not get price for {symbol}: {data['error']}",
            error=data["error"],
        )
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
        return FastResult(text=f"No price data for {symbol}", error="no_data")
    msg = f"{symbol}: ${float(price):,.2f}"
    if change is not None:
        try:
            sign = "+" if float(change) >= 0 else ""
            msg += f" ({sign}{float(change):.2f}% 24h)"
        except (TypeError, ValueError):
            pass
    return FastResult(text=msg)


async def cmd_ta(arg: str, mcp: MCPRegistry, p: Persistence) -> FastResult:
    """``/ta <symbol>`` — combined token analysis (price + market cap + liquidity)."""
    parts = arg.strip().split()
    if not parts:
        return FastResult(text="Usage: /ta <symbol>\nExample: /ta BTC")
    symbol = parts[0].upper()
    try:
        raw = await mcp.call("combined_token_analysis", {"symbol": symbol})
    except UnknownTool:
        return FastResult(
            text="TA not available (combined_token_analysis MCP missing).",
            error="missing_tool",
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return FastResult(text="Could not parse TA data.", error="bad_response")
    if "error" in data:
        return FastResult(text=f"Error: {data['error']}", error=data["error"])

    lines = [f"TA: {data.get('symbol', symbol)}"]
    if data.get("current_price_usd") is not None:
        lines.append(f"Price: ${float(data['current_price_usd']):,.2f}")
    if data.get("market_cap_usd") is not None:
        lines.append(f"Market cap: ${float(data['market_cap_usd']):,.0f}")
    if data.get("liquidity_usd") is not None:
        lines.append(f"Liquidity: ${float(data['liquidity_usd']):,.0f}")
    if data.get("volume_24h_usd") is not None:
        lines.append(f"24h volume: ${float(data['volume_24h_usd']):,.0f}")
    if data.get("price_change_24h_pct") is not None:
        try:
            ch = float(data["price_change_24h_pct"])
            sign = "+" if ch >= 0 else ""
            lines.append(f"24h change: {sign}{ch:.2f}%")
        except (TypeError, ValueError):
            pass
    if len(lines) == 1:  # only the header — no data fields populated
        lines.append("(no analysis fields returned)")
    return FastResult(text="\n".join(lines))


async def cmd_caps(arg: str, mcp: MCPRegistry, p: Persistence) -> FastResult:
    """``/caps`` — today's spend, no MCP, no LLM."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    global_spend = p.spend_today(today)
    return FastResult(text=f"Today's spend (UTC {today}): ${global_spend:.4f}")


async def cmd_help(arg: str, mcp: MCPRegistry, p: Persistence) -> FastResult:
    """``/help`` — list fast commands."""
    return FastResult(text=(
        "Fast commands (bypass the agent loop — instant, $0):\n"
        "  /price <symbol>      Live price + 24h change\n"
        "  /ta <symbol>         Token analysis (price, market cap, liquidity)\n"
        "  /caps                Today's spend\n"
        "  /help                This message\n"
        "\n"
        "Anything else goes through the agent loop. Prefix with /quick or\n"
        "/deep to force tier-S or tier-L routing."
    ))


# ── Dispatcher ───────────────────────────────────────────────────────────


COMMANDS: dict[str, CommandHandler] = {
    "/price": cmd_price,
    "/ta": cmd_ta,
    "/caps": cmd_caps,
    "/help": cmd_help,
}


def is_fast_command(message: str) -> bool:
    msg = (message or "").strip()
    for cmd in COMMANDS:
        if msg == cmd or msg.startswith(cmd + " "):
            return True
    return False


async def dispatch(
    message: str,
    *,
    mcp: MCPRegistry,
    persistence: Persistence,
) -> FastResult | None:
    """Run a fast command, or return None if nothing matched."""
    msg = (message or "").strip()
    for cmd, handler in COMMANDS.items():
        if msg == cmd:
            return await handler("", mcp, persistence)
        if msg.startswith(cmd + " "):
            return await handler(msg[len(cmd) + 1:].strip(), mcp, persistence)
    return None
