"""Telegram-out MCP — agent can DM whitelisted chats only (I8).

Env:
  TELEGRAM_BOT_TOKEN
  LONGAI_OUTBOUND_WHITELIST  (comma-separated chat_ids)
"""
from __future__ import annotations

import asyncio
import json
import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


def _whitelist() -> set[int]:
    raw = os.environ.get("LONGAI_OUTBOUND_WHITELIST", "").strip()
    if not raw:
        return set()
    return {int(x) for x in raw.split(",") if x.strip()}


async def _send_message_impl(chat_id: int, text: str) -> dict:
    """The whitelist gate. Returns dict (will be JSON-serialized)."""
    if chat_id not in _whitelist():
        return {"error": "chat_id not in outbound whitelist"}
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return {"error": "TELEGRAM_BOT_TOKEN not set"}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(url, json={"chat_id": chat_id, "text": text})
    try:
        r.raise_for_status()
        return {"ok": True}
    except httpx.HTTPError as e:
        return {"error": str(e)}


server = Server("longai-telegram-out")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(
        name="send_message",
        description="Send a Telegram message to a whitelisted chat_id. "
                    "Unauthorized destinations are rejected at the tool layer.",
        inputSchema={
            "type": "object",
            "properties": {"chat_id": {"type": "integer"}, "text": {"type": "string"}},
            "required": ["chat_id", "text"],
        },
    )]


@server.call_tool()
async def call_tool(name: str, args: dict) -> list[TextContent]:
    if name != "send_message":
        return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]
    result = await _send_message_impl(int(args["chat_id"]), str(args["text"]))
    return [TextContent(type="text", text=json.dumps(result))]


async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
