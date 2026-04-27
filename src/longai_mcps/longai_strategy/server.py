"""LongAI strategy MCP wrapper — multi-timeframe TA.

Tool: analyze_ticker(symbol, timeframes=[4h,1d,1w]).

Requires optional [longai_strategy] extra: yfinance, pandas, numpy.
"""
from __future__ import annotations

import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


def _td_sequential(closes: list[float]) -> int:
    """Simplified TD9 setup count: bars where close < close[4] back."""
    if len(closes) < 9:
        return 0
    count = 0
    for i in range(8, len(closes)):
        if closes[i] < closes[i - 4]:
            count += 1
        else:
            count = 0
    return count


def _bb_position(closes: list[float], period: int = 20) -> float:
    """Bollinger band position 0..1; 0=lower band, 0.5=middle, 1=upper band."""
    if len(closes) < period:
        return 0.5
    import statistics
    window = closes[-period:]
    mean = statistics.fmean(window)
    sd = statistics.pstdev(window)
    upper = mean + 2 * sd
    lower = mean - 2 * sd
    if upper == lower:
        return 0.5
    return max(0.0, min(1.0, (closes[-1] - lower) / (upper - lower)))


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


async def _fetch_closes(symbol: str, interval: str) -> list[float]:
    """yfinance wrapper. Sync call, run in thread."""
    import yfinance as yf
    period_map = {"4h": "60d", "1d": "1y", "1w": "5y"}
    interval_map = {"4h": "1h", "1d": "1d", "1w": "1wk"}  # yfinance lacks 4h; aggregate from 1h
    period = period_map.get(interval, "1y")
    yf_interval = interval_map.get(interval, "1d")

    def _do():
        df = yf.download(symbol, period=period, interval=yf_interval, progress=False)
        if df.empty:
            return []
        if interval == "4h":
            # naive 4h aggregation from 1h closes
            df = df["Close"].iloc[::4]
        else:
            df = df["Close"]
        return list(df.values.tolist())

    return await asyncio.to_thread(_do)


def _analyze_one(closes: list[float]) -> dict:
    if not closes:
        return {"error": "no data"}
    return {
        "rsi": round(_rsi(closes), 1),
        "bb_position": round(_bb_position(closes), 2),
        "td9_setup": _td_sequential(closes),
        "last_close": round(closes[-1], 4),
    }


server = Server("longai-longai-strategy")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(
        name="analyze_ticker",
        description="Multi-timeframe TA: TD9 setup, RSI, Bollinger position across "
                    "4h/1d/1w. Returns confluence scoring across timeframes.",
        inputSchema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "yfinance ticker (e.g., BTC-USD, AAPL)"},
                "timeframes": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["4h", "1d", "1w"]},
                    "default": ["4h", "1d", "1w"],
                },
            },
            "required": ["symbol"],
        },
    )]


@server.call_tool()
async def call_tool(name: str, args: dict) -> list[TextContent]:
    if name != "analyze_ticker":
        return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]
    symbol = args.get("symbol", "")
    tfs = args.get("timeframes") or ["4h", "1d", "1w"]
    out: dict = {"symbol": symbol, "timeframes": {}}
    try:
        for tf in tfs:
            closes = await _fetch_closes(symbol, tf)
            out["timeframes"][tf] = _analyze_one(closes)
        # Confluence: average BB pos + RSI direction across non-empty timeframes
        valid = [v for v in out["timeframes"].values() if "error" not in v]
        if valid:
            out["confluence"] = {
                "avg_rsi": round(sum(v["rsi"] for v in valid) / len(valid), 1),
                "avg_bb_position": round(sum(v["bb_position"] for v in valid) / len(valid), 2),
                "any_overbought": any(v["rsi"] > 70 for v in valid),
                "any_oversold": any(v["rsi"] < 30 for v in valid),
            }
        return [TextContent(type="text", text=json.dumps(out))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
