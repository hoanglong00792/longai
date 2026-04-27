"""On-chain TA MCP wrapper.

Tools:
  - coingecko_token_info(chain, address)
  - dexscreener_pairs(chain, address)
  - combined_token_analysis(chain, address)

Note: requires the optional `requests` extra (per pyproject.toml [on_chain_ta]).
"""
from __future__ import annotations

import asyncio
import json

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


CHAIN_MAP = {
    "ethereum": "ethereum", "eth": "ethereum",
    "polygon": "polygon-pos", "matic": "polygon-pos",
    "arbitrum": "arbitrum-one", "arb": "arbitrum-one",
    "base": "base", "bsc": "binance-smart-chain", "bnb": "binance-smart-chain",
    "optimism": "optimistic-ethereum", "op": "optimistic-ethereum",
    "avalanche": "avalanche", "avax": "avalanche",
}


async def _coingecko(chain: str, address: str) -> dict:
    cg_chain = CHAIN_MAP.get(chain.lower(), chain.lower())
    url = f"https://api.coingecko.com/api/v3/coins/{cg_chain}/contract/{address}"
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(url)
    if r.status_code == 404:
        return {"error": f"token not found on {chain}"}
    r.raise_for_status()
    d = r.json()
    return {
        "name": d.get("name"), "symbol": d.get("symbol"),
        "market_cap_usd": d.get("market_data", {}).get("market_cap", {}).get("usd"),
        "price_usd": d.get("market_data", {}).get("current_price", {}).get("usd"),
        "volume_24h_usd": d.get("market_data", {}).get("total_volume", {}).get("usd"),
        "circulating_supply": d.get("market_data", {}).get("circulating_supply"),
    }


async def _dexscreener(chain: str, address: str) -> dict:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(url)
    r.raise_for_status()
    d = r.json()
    pairs = d.get("pairs") or []
    chain_pairs = [p for p in pairs if p.get("chainId", "").lower() == chain.lower()][:5]
    return {
        "pair_count": len(pairs),
        "top_pairs": [{
            "dex": p.get("dexId"), "pair": f"{p['baseToken']['symbol']}/{p['quoteToken']['symbol']}",
            "liquidity_usd": p.get("liquidity", {}).get("usd"),
            "volume_24h_usd": p.get("volume", {}).get("h24"),
            "price_change_24h": p.get("priceChange", {}).get("h24"),
        } for p in chain_pairs],
    }


server = Server("longai-on-chain-ta")


@server.list_tools()
async def list_tools() -> list[Tool]:
    addr_props = {"chain": {"type": "string"}, "address": {"type": "string"}}
    return [
        Tool(name="coingecko_token_info",
             description="Fetch token fundamentals from CoinGecko by contract address.",
             inputSchema={"type": "object", "properties": addr_props,
                          "required": ["chain", "address"]}),
        Tool(name="dexscreener_pairs",
             description="Fetch DEX trading pairs from DexScreener (no Playwright required).",
             inputSchema={"type": "object", "properties": addr_props,
                          "required": ["chain", "address"]}),
        Tool(name="combined_token_analysis",
             description="Run both CoinGecko + DexScreener and merge results.",
             inputSchema={"type": "object", "properties": addr_props,
                          "required": ["chain", "address"]}),
    ]


@server.call_tool()
async def call_tool(name: str, args: dict) -> list[TextContent]:
    chain = args.get("chain", "")
    address = args.get("address", "")
    try:
        if name == "coingecko_token_info":
            return [TextContent(type="text", text=json.dumps(await _coingecko(chain, address)))]
        if name == "dexscreener_pairs":
            return [TextContent(type="text", text=json.dumps(await _dexscreener(chain, address)))]
        if name == "combined_token_analysis":
            cg, ds = await asyncio.gather(
                _coingecko(chain, address), _dexscreener(chain, address),
                return_exceptions=True,
            )
            return [TextContent(type="text", text=json.dumps({
                "coingecko": cg if not isinstance(cg, Exception) else {"error": str(cg)},
                "dexscreener": ds if not isinstance(ds, Exception) else {"error": str(ds)},
            }))]
        return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
