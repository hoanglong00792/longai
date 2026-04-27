"""Web-search MCP — Tavily-backed AI search with key rotation.

Why this exists:
  Playwright works for arbitrary pages but is slow (~10-30s) and dumps raw DOM.
  Tavily returns a synthesized 1-paragraph answer plus 3 ranked sources in
  ~1-2s — token-cheap and good enough for most "what is X / latest news Y"
  questions. Agent uses this as the cheap default, escalates to Playwright
  only for interactive / login-walled / chart-heavy pages.

Env:
  TAVILY_API_KEYS   comma-separated list (rotates on 429/432/433)
  TAVILY_API_KEY    single key (legacy fallback)

Free tier: 1000 credits/month per key, basic search = 1 credit. Multiple
keys multiply the budget at zero code cost.

Tools:
  - web_search(query, num_results=3, topic="auto"|"general"|"finance")
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logger = logging.getLogger("longai_mcps.web_search")

# Deterministic safety filter — never LLM-decided.
_BLOCKLIST = frozenset({
    "hack", "exploit", "crack", "bypass", "injection",
    "ddos", "attack", "vulnerability", "0day",
    "malware", "virus", "trojan", "rootkit", "backdoor",
    "phishing", "darknet", "dark web",
    "bomb", "explosive", "weapon",
})

# Topic auto-detect: finance keywords flip Tavily into 'finance' mode for
# better quote-data + financial-news ranking.
_FINANCE_HINTS = frozenset({
    "btc", "eth", "sol", "gold", "stock", "crypto", "price", "market",
    "vnindex", "spx", "nasdaq", "coin", "token", "defi", "dex",
    "bull", "bear", "rally", "dump", "pump", "yield", "apy",
})


def _api_keys() -> list[str]:
    """Return all Tavily keys; rotates over them on rate-limit."""
    raw = os.environ.get("TAVILY_API_KEYS", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        single = os.environ.get("TAVILY_API_KEY", "").strip()
        if single:
            keys = [single]
    return keys


def _is_safe(query: str) -> bool:
    lower = query.lower()
    return not any(b in lower for b in _BLOCKLIST)


def _detect_topic(query: str) -> str:
    return "finance" if any(w in query.lower() for w in _FINANCE_HINTS) else "general"


# Module-level rotation index — survives across calls within one server lifetime.
_key_index = 0


async def _tavily_search(query: str, num_results: int, topic: str) -> dict:
    """POST to Tavily; rotate keys on rate-limit. Returns parsed dict or error.

    Tavily response shape (relevant fields):
        {"answer": str, "results": [{"title", "url", "content", "score"}, ...]}
    """
    global _key_index
    keys = _api_keys()
    if not keys:
        return {"error": "TAVILY_API_KEYS (or TAVILY_API_KEY) not set"}

    tried = 0
    last_error = ""
    async with httpx.AsyncClient(timeout=15.0) as client:
        while tried < len(keys):
            key = keys[_key_index % len(keys)]
            try:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "query": query,
                        "max_results": num_results,
                        "search_depth": "basic",
                        "topic": topic,
                        "include_answer": True,
                    },
                )
            except httpx.HTTPError as e:
                last_error = f"network: {e}"
                _key_index += 1
                tried += 1
                continue
            # Rate / credit limits → rotate to next key
            if resp.status_code in (429, 432, 433):
                logger.warning("tavily key #%d hit %d, rotating",
                               _key_index % len(keys), resp.status_code)
                _key_index += 1
                tried += 1
                last_error = f"rate-limit ({resp.status_code})"
                continue
            if resp.status_code >= 500:
                last_error = f"server error ({resp.status_code})"
                _key_index += 1
                tried += 1
                continue
            if resp.status_code >= 400:
                # 401 (bad key), 403 (suspended), etc — try next key
                last_error = f"auth/client error ({resp.status_code})"
                _key_index += 1
                tried += 1
                continue
            return resp.json()

    return {"error": f"all {len(keys)} keys exhausted; last={last_error}"}


def _format_results(data: dict, query: str) -> dict:
    """Pack Tavily output into a tight JSON shape for the agent."""
    if "error" in data:
        return data
    answer = (data.get("answer") or "").strip()
    raw_results = data.get("results") or []
    results = [{
        "rank": i + 1,
        "title": r.get("title", "")[:140],
        "url": r.get("url", ""),
        "snippet": (r.get("content") or "")[:400],
    } for i, r in enumerate(raw_results)]
    out = {"query": query}
    if answer:
        out["answer"] = answer
    out["results"] = results
    return out


# ── MCP server wiring ──────────────────────────────────────────────────────

server = Server("longai-web-search")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(
        name="web_search",
        description=(
            "AI-summarized web search via Tavily. Returns a synthesized answer "
            "(when available) plus 3 ranked sources with snippets. Fast (~1-2s), "
            "token-cheap. Prefer this over browser navigation for general "
            "knowledge questions, news, and quick lookups. Topic auto-detects "
            "'finance' for crypto/stock queries; pass topic='general' to override."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "num_results": {"type": "integer", "default": 3, "minimum": 1, "maximum": 10},
                "topic": {
                    "type": "string",
                    "enum": ["auto", "general", "finance"],
                    "default": "auto",
                },
            },
            "required": ["query"],
        },
    )]


@server.call_tool()
async def call_tool(name: str, args: dict) -> list[TextContent]:
    if name != "web_search":
        return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]
    query = (args.get("query") or "").strip()
    if not query:
        return [TextContent(type="text", text=json.dumps({"error": "empty query"}))]
    if not _is_safe(query):
        return [TextContent(type="text", text=json.dumps({"error": "query contains blocked term"}))]

    num_results = max(1, min(int(args.get("num_results", 3)), 10))
    topic_arg = args.get("topic", "auto")
    topic = _detect_topic(query) if topic_arg == "auto" else str(topic_arg)

    data = await _tavily_search(query, num_results, topic)
    return [TextContent(type="text", text=json.dumps(_format_results(data, query)))]


async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
