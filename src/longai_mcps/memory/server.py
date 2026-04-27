# src/longai_mcps/memory/server.py
"""MCP server exposing recall_memory and save_memory.

Run as: python -m longai_mcps.memory.server
Uses LONGAI_DB_PATH env var to point at the same sqlite file as the bot.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from longai.memory import Memory
from longai.persistence import Persistence


_p: Persistence | None = None
_m: Memory | None = None


def _ensure_open():
    global _p, _m
    if _p is None:
        path = os.environ.get("LONGAI_DB_PATH", "~/.longai/state.db")
        _p = Persistence(path); _p.init()
        _m = Memory(_p)


server = Server("longai-memory")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="recall_memory",
            description="Retrieve up to 5 stored memories matching a query.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "type": {"type": "string", "enum": ["preference", "domain"]},
                    "limit": {"type": "integer", "default": 5, "maximum": 10},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="save_memory",
            description="Save a memory entry (preference or domain knowledge).",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "type": {"type": "string", "enum": ["preference", "domain"]},
                },
                "required": ["text", "type"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, args: dict) -> list[TextContent]:
    _ensure_open()
    assert _m is not None
    chat_id = int(os.environ.get("LONGAI_CURRENT_CHAT_ID", "0")) or None

    if name == "recall_memory":
        rows = _m.recall(
            query=args.get("query"), chat_id=chat_id,
            type=args.get("type"), limit=int(args.get("limit", 5)),
        )
        if not rows:
            return [TextContent(type="text", text=json.dumps({
                "results": [],
                "hint": "No memories matched. Save one with save_memory if needed.",
            }))]
        return [TextContent(type="text", text=json.dumps({
            "results": [{"content": r["content"], "type": r["type"]} for r in rows]
        }))]

    if name == "save_memory":
        t = args.get("type")
        if t not in ("preference", "domain"):
            return [TextContent(type="text", text=json.dumps({
                "error": "invalid memory type (must be 'preference' or 'domain')",
            }))]
        text = args.get("text", "").strip()
        if not text:
            return [TextContent(type="text", text=json.dumps({"error": "empty text"}))]
        mid = _m.save(
            type=t, content=text[:500], chat_id=chat_id,
            source="agent_tool", applied_by="agent", ts=int(time.time()),
        )
        return [TextContent(type="text", text=json.dumps({"ok": True, "id": mid}))]

    return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]


async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
