"""EVM MCP — read-only RPC + Etherscan ABI decode.

Env vars:
  LONGAI_RPC_URL_MAINNET  (e.g., https://eth.llamarpc.com)
  LONGAI_RPC_URL_BASE
  LONGAI_RPC_URL_ARBITRUM
  ETHERSCAN_API_KEY
"""
from __future__ import annotations

import asyncio
import json
import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


CHAIN_RPC_ENV = {
    "ethereum": "LONGAI_RPC_URL_MAINNET",
    "base": "LONGAI_RPC_URL_BASE",
    "arbitrum": "LONGAI_RPC_URL_ARBITRUM",
}


def _rpc_url(chain: str) -> str:
    env = CHAIN_RPC_ENV.get(chain.lower())
    if not env:
        raise ValueError(f"unknown chain: {chain}")
    url = os.environ.get(env)
    if not url:
        raise ValueError(f"{env} not set")
    return url


async def _rpc(chain: str, method: str, params: list) -> dict:
    url = _rpc_url(chain)
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(url, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
        })
    r.raise_for_status()
    return r.json()


server = Server("longai-evm")


@server.list_tools()
async def list_tools() -> list[Tool]:
    addr_props = {"chain": {"type": "string"}, "address": {"type": "string"}}
    block_props = {**addr_props, "block": {"type": "string", "default": "latest"}}
    return [
        Tool(name="eth_getBalance",
             description="Get ETH/native balance for an address.",
             inputSchema={"type": "object", "properties": block_props,
                          "required": ["chain", "address"]}),
        Tool(name="eth_getCode",
             description="Get bytecode at a contract address.",
             inputSchema={"type": "object", "properties": block_props,
                          "required": ["chain", "address"]}),
        Tool(name="eth_call",
             description="Call a contract method (read-only).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "chain": {"type": "string"}, "to": {"type": "string"},
                     "data": {"type": "string"}, "block": {"type": "string", "default": "latest"},
                 },
                 "required": ["chain", "to", "data"],
             }),
        Tool(name="eth_getStorageAt",
             description="Read raw storage at a slot.",
             inputSchema={
                 "type": "object",
                 "properties": {**addr_props, "slot": {"type": "string"},
                                "block": {"type": "string", "default": "latest"}},
                 "required": ["chain", "address", "slot"],
             }),
        Tool(name="etherscan_get_abi",
             description="Fetch a verified contract ABI from Etherscan.",
             inputSchema={"type": "object", "properties": addr_props,
                          "required": ["chain", "address"]}),
    ]


@server.call_tool()
async def call_tool(name: str, args: dict) -> list[TextContent]:
    try:
        if name == "eth_getBalance":
            res = await _rpc(args["chain"], "eth_getBalance",
                             [args["address"], args.get("block", "latest")])
            return [TextContent(type="text", text=json.dumps(res))]
        if name == "eth_getCode":
            res = await _rpc(args["chain"], "eth_getCode",
                             [args["address"], args.get("block", "latest")])
            return [TextContent(type="text", text=json.dumps(res))]
        if name == "eth_call":
            res = await _rpc(args["chain"], "eth_call",
                             [{"to": args["to"], "data": args["data"]},
                              args.get("block", "latest")])
            return [TextContent(type="text", text=json.dumps(res))]
        if name == "eth_getStorageAt":
            res = await _rpc(args["chain"], "eth_getStorageAt",
                             [args["address"], args["slot"], args.get("block", "latest")])
            return [TextContent(type="text", text=json.dumps(res))]
        if name == "etherscan_get_abi":
            key = os.environ.get("ETHERSCAN_API_KEY")
            if not key:
                return [TextContent(type="text", text=json.dumps({"error": "ETHERSCAN_API_KEY not set"}))]
            chain_to_host = {
                "ethereum": "api.etherscan.io", "base": "api.basescan.org",
                "arbitrum": "api.arbiscan.io",
            }
            host = chain_to_host.get(args["chain"].lower())
            if not host:
                return [TextContent(type="text", text=json.dumps({"error": f"unsupported chain: {args['chain']}"}))]
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(f"https://{host}/api", params={
                    "module": "contract", "action": "getabi",
                    "address": args["address"], "apikey": key,
                })
            return [TextContent(type="text", text=r.text)]
        return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
