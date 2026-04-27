"""URL fetch MCP — clean text excerpt for any URL.

Why this exists:
  - x.com / twitter.com block generic scrapers (login wall). Solution: route
    through api.vxtwitter.com which mirrors FixTweet/FxTwitter — returns JSON
    with text, author, counts, media. No auth required.
  - Generic URLs need main-content extraction (strip nav/ads/footer). We use
    `trafilatura` (pure-Python, ~5MB, MIT) for that, with a regex fallback.
  - Result is capped at max_chars and wrapped with delimiters for safe
    injection into LLM context (the Loop's sanitize_tool_output adds I11
    redaction on top).

Tools:
  - fetch_url(url, max_chars=2000)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logger = logging.getLogger("longai_mcps.url_fetch")

_TWITTER_HOST = re.compile(
    r"^https?://(?:www\.)?(?:x|twitter)\.com/",
    re.IGNORECASE,
)

# Same blocklist as web_search — never fetch unsafe URLs.
_URL_BLOCKLIST = frozenset({
    "darknet", "dark web", "0day", "exploit-db",
})


def _safe_url(url: str) -> bool:
    lower = url.lower()
    return not any(b in lower for b in _URL_BLOCKLIST)


# ── Twitter / X via vxtwitter ─────────────────────────────────────────────


async def _fetch_tweet(url: str, max_chars: int) -> dict:
    """Rewrite to api.vxtwitter.com host, fetch JSON, format for LLM context."""
    api_url = re.sub(
        r"^(https?://)(?:www\.)?(?:x|twitter)\.com/",
        r"\1api.vxtwitter.com/",
        url, count=1, flags=re.IGNORECASE,
    )
    if api_url == url:
        return {"error": "url did not match Twitter pattern"}

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(api_url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("vxtwitter failed for %s: %s", url[:60], e)
        return {"error": f"vxtwitter fetch failed: {e}"}

    text = (data.get("text") or "").strip()
    media = data.get("mediaURLs") or []
    if not text and not media:
        return {"error": "tweet empty (deleted or invalid id)"}

    parts = [
        f"Tweet by {data.get('user_name') or '?'} (@{data.get('user_screen_name') or '?'})"
    ]
    if data.get("date"):
        parts.append(f"Posted: {data['date']}")
    if text:
        parts.append("")
        parts.append(text)
    likes = data.get("likes", 0)
    retweets = data.get("retweets", 0)
    if likes or retweets:
        parts.append("")
        parts.append(f"({likes} likes, {retweets} retweets)")
    if media:
        parts.append("")
        parts.append(f"Media: {', '.join(media[:4])}")

    body = "\n".join(parts)
    if len(body) > max_chars:
        body = body[:max_chars].rsplit(" ", 1)[0] + " …"

    return {
        "type": "tweet",
        "source_url": url,
        "content": body,
    }


# ── Generic URL via trafilatura with regex fallback ────────────────────────


async def _fetch_html(url: str) -> str:
    async with httpx.AsyncClient(
        timeout=10.0, follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; longai/1.0)"},
    ) as client:
        resp = await client.get(url)
    resp.raise_for_status()
    return resp.text


def _extract_with_trafilatura(html: str, url: str) -> tuple[str, str]:
    """Return (title, body). Empty-string fallbacks if trafilatura fails."""
    try:
        import trafilatura
    except ImportError:
        return "", ""
    body = trafilatura.extract(
        html, url=url,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
        favor_recall=False,
    ) or ""
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:120]
    return title, body


def _extract_with_regex(html: str) -> tuple[str, str]:
    """Last-ditch HTML strip when trafilatura is unavailable or returns empty."""
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:120]
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                  flags=re.IGNORECASE | re.DOTALL)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return title, body


async def _fetch_generic(url: str, max_chars: int) -> dict:
    try:
        html = await _fetch_html(url)
    except httpx.HTTPError as e:
        return {"error": f"fetch failed: {e}"}

    title, body = _extract_with_trafilatura(html, url)
    method = "trafilatura"
    if not body:
        title, body = _extract_with_regex(html)
        method = "regex-fallback"
    if not body:
        return {"error": "empty body after extraction"}
    if len(body) > max_chars:
        body = body[:max_chars].rsplit(" ", 1)[0] + " …"
    return {
        "type": "page",
        "source_url": url,
        "title": title,
        "content": body,
        "extraction": method,
    }


# ── MCP server wiring ──────────────────────────────────────────────────────

server = Server("longai-url-fetch")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(
        name="fetch_url",
        description=(
            "Fetch a URL and return its main content as cleaned text. "
            "Twitter / X URLs route through api.vxtwitter.com (the main site "
            "blocks scrapers). All other URLs use trafilatura for "
            "main-content extraction (strips nav/ads/footer), with a regex "
            "fallback. Returns JSON: {type, source_url, title?, content}. "
            "Capped at max_chars (default 2000). Fast (~0.3-2s); use this "
            "before reaching for Playwright on plain article pages."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_chars": {"type": "integer", "default": 2000, "minimum": 200, "maximum": 8000},
            },
            "required": ["url"],
        },
    )]


@server.call_tool()
async def call_tool(name: str, args: dict) -> list[TextContent]:
    if name != "fetch_url":
        return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]
    url = (args.get("url") or "").strip()
    if not url:
        return [TextContent(type="text", text=json.dumps({"error": "empty url"}))]
    if not _safe_url(url):
        return [TextContent(type="text", text=json.dumps({"error": "url contains blocked term"}))]
    max_chars = max(200, min(int(args.get("max_chars", 2000)), 8000))

    if _TWITTER_HOST.match(url):
        result = await _fetch_tweet(url, max_chars)
    else:
        result = await _fetch_generic(url, max_chars)
    return [TextContent(type="text", text=json.dumps(result))]


async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
