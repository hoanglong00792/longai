"""url_fetch MCP — vxtwitter routing + trafilatura/regex extraction."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from longai_mcps.url_fetch.server import (
    _TWITTER_HOST,
    _extract_with_regex,
    _fetch_generic,
    _fetch_tweet,
    _safe_url,
)


# ---- Twitter host detection ----

@pytest.mark.parametrize("url,is_twitter", [
    ("https://x.com/user/status/1234", True),
    ("https://twitter.com/user/status/1234", True),
    ("https://www.x.com/user", True),
    ("http://x.com/u", True),
    ("https://example.com/x.com/page", False),
    ("https://github.com/twitter/something", False),
])
def test_twitter_host_regex(url, is_twitter):
    assert bool(_TWITTER_HOST.match(url)) == is_twitter


# ---- Safety blocklist ----

def test_safe_url():
    assert _safe_url("https://example.com")
    assert not _safe_url("https://exploit-db.com/anything")
    assert not _safe_url("https://darknet.example.org")


# ---- Twitter fetch ----

def _mk_resp(status=200, json_body=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.text = text
    if json_body is not None:
        r.json = MagicMock(return_value=json_body)
    r.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("err", request=None, response=None)
        if status >= 400 else lambda: None
    )
    return r


@pytest.mark.asyncio
async def test_fetch_tweet_happy_path():
    body = {
        "user_name": "Alice",
        "user_screen_name": "alice_handle",
        "text": "Hello world from a tweet",
        "likes": 42,
        "retweets": 7,
        "date": "2026-04-25",
        "mediaURLs": ["https://media.tw/img1.jpg"],
    }
    fake = _mk_resp(200, json_body=body)
    with patch("longai_mcps.url_fetch.server.httpx.AsyncClient") as MockClient:
        client = AsyncMock()
        client.get = AsyncMock(return_value=fake)
        MockClient.return_value.__aenter__.return_value = client
        result = await _fetch_tweet("https://x.com/alice/status/1234", max_chars=2000)

    assert result["type"] == "tweet"
    assert result["source_url"] == "https://x.com/alice/status/1234"
    content = result["content"]
    assert "Alice" in content
    assert "@alice_handle" in content
    assert "Hello world" in content
    assert "42 likes" in content
    assert "7 retweets" in content
    assert "img1.jpg" in content


@pytest.mark.asyncio
async def test_fetch_tweet_url_rewrite_to_vxtwitter():
    """Verify the URL passed to httpx is api.vxtwitter.com, not x.com."""
    captured: dict = {}

    async def capture_get(url, **kw):
        captured["url"] = url
        return _mk_resp(200, json_body={"text": "x", "user_name": "u",
                                         "user_screen_name": "h"})

    with patch("longai_mcps.url_fetch.server.httpx.AsyncClient") as MockClient:
        client = AsyncMock()
        client.get = AsyncMock(side_effect=capture_get)
        MockClient.return_value.__aenter__.return_value = client
        await _fetch_tweet("https://twitter.com/foo/status/9", max_chars=2000)

    assert "api.vxtwitter.com" in captured["url"]
    assert "twitter.com" not in captured["url"].replace("api.vxtwitter.com", "")
    assert "/foo/status/9" in captured["url"]


@pytest.mark.asyncio
async def test_fetch_tweet_empty_returns_error():
    body = {"user_name": "u", "user_screen_name": "h", "text": "", "mediaURLs": []}
    fake = _mk_resp(200, json_body=body)
    with patch("longai_mcps.url_fetch.server.httpx.AsyncClient") as MockClient:
        client = AsyncMock()
        client.get = AsyncMock(return_value=fake)
        MockClient.return_value.__aenter__.return_value = client
        result = await _fetch_tweet("https://x.com/u/status/1", max_chars=2000)
    assert "error" in result


# ---- Regex extraction ----

def test_regex_extracts_title_and_strips_tags():
    html = """
    <html>
      <head><title>Hello World</title></head>
      <body>
        <script>alert('x')</script>
        <p>The quick brown fox</p>
        <style>.x{}</style>
      </body>
    </html>
    """
    title, body = _extract_with_regex(html)
    assert title == "Hello World"
    assert "The quick brown fox" in body
    assert "alert" not in body
    assert ".x{}" not in body


def test_regex_no_title():
    html = "<html><body>just text</body></html>"
    title, body = _extract_with_regex(html)
    assert title == ""
    assert "just text" in body


# ---- Generic fetch ----

@pytest.mark.asyncio
async def test_fetch_generic_caps_at_max_chars():
    big_html = "<html><head><title>T</title></head><body>" + ("blah " * 1000) + "</body></html>"
    fake = _mk_resp(200, text=big_html)
    with patch("longai_mcps.url_fetch.server.httpx.AsyncClient") as MockClient:
        client = AsyncMock()
        client.get = AsyncMock(return_value=fake)
        MockClient.return_value.__aenter__.return_value = client
        result = await _fetch_generic("https://example.com", max_chars=500)
    assert "error" not in result
    assert len(result["content"]) <= 510  # 500 + ellipsis headroom


@pytest.mark.asyncio
async def test_fetch_generic_http_error_returns_error():
    with patch("longai_mcps.url_fetch.server.httpx.AsyncClient") as MockClient:
        client = AsyncMock()
        client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
        MockClient.return_value.__aenter__.return_value = client
        result = await _fetch_generic("https://nonexistent.invalid", max_chars=2000)
    assert "error" in result
    assert "fetch failed" in result["error"]
