"""web_search MCP — Tavily wrapper with key rotation and safety filter."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from longai_mcps.web_search.server import (
    _detect_topic,
    _format_results,
    _is_safe,
    _tavily_search,
)


# ---- Safety + topic ----

def test_is_safe_clean_query():
    assert _is_safe("what is bitcoin")
    assert _is_safe("ETH price today")


def test_is_safe_blocks_unsafe():
    assert not _is_safe("how to hack a server")
    assert not _is_safe("phishing tutorial")
    assert not _is_safe("buy weapon online")


def test_topic_finance_detected():
    assert _detect_topic("BTC price right now") == "finance"
    assert _detect_topic("nasdaq today") == "finance"
    assert _detect_topic("apy on aave") == "finance"


def test_topic_general_default():
    assert _detect_topic("what is python") == "general"
    assert _detect_topic("how to bake bread") == "general"


# ---- Result formatting ----

def test_format_results_with_answer():
    raw = {
        "answer": "Python is a programming language.",
        "results": [
            {"title": "Python.org", "url": "https://python.org", "content": "official site", "score": 0.9},
        ],
    }
    out = _format_results(raw, "python")
    assert out["answer"] == "Python is a programming language."
    assert out["results"][0]["rank"] == 1
    assert out["results"][0]["title"] == "Python.org"
    assert out["query"] == "python"


def test_format_results_truncates_long_snippets():
    raw = {
        "answer": "",
        "results": [{"title": "T", "url": "u", "content": "x" * 1000}],
    }
    out = _format_results(raw, "q")
    assert len(out["results"][0]["snippet"]) == 400


def test_format_results_propagates_error():
    raw = {"error": "no keys"}
    out = _format_results(raw, "q")
    assert out == {"error": "no keys"}


# ---- Tavily search ----

def _mk_response(status, json_body):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=json_body)
    return r


@pytest.mark.asyncio
async def test_tavily_no_keys_returns_error(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEYS", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    result = await _tavily_search("hello", 3, "general")
    assert "error" in result
    assert "not set" in result["error"]


@pytest.mark.asyncio
async def test_tavily_happy_path(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    fake_resp = _mk_response(200, {
        "answer": "ok",
        "results": [{"title": "A", "url": "https://a", "content": "b"}],
    })
    with patch("longai_mcps.web_search.server.httpx.AsyncClient") as MockClient:
        mock = AsyncMock()
        mock.post = AsyncMock(return_value=fake_resp)
        MockClient.return_value.__aenter__.return_value = mock
        result = await _tavily_search("hello", 3, "general")
    assert result["answer"] == "ok"


@pytest.mark.asyncio
async def test_tavily_rate_limit_rotates_to_next_key(monkeypatch):
    """429 on key #1 → try key #2 → success."""
    monkeypatch.setenv("TAVILY_API_KEYS", "key-a,key-b")
    rate_resp = _mk_response(429, {})
    ok_resp = _mk_response(200, {"answer": "ok2", "results": []})

    with patch("longai_mcps.web_search.server.httpx.AsyncClient") as MockClient:
        mock = AsyncMock()
        mock.post = AsyncMock(side_effect=[rate_resp, ok_resp])
        MockClient.return_value.__aenter__.return_value = mock
        result = await _tavily_search("hello", 3, "general")
    assert result["answer"] == "ok2"


@pytest.mark.asyncio
async def test_tavily_all_keys_exhausted(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEYS", "key-a,key-b")
    rate_resp = _mk_response(429, {})

    with patch("longai_mcps.web_search.server.httpx.AsyncClient") as MockClient:
        mock = AsyncMock()
        mock.post = AsyncMock(return_value=rate_resp)
        MockClient.return_value.__aenter__.return_value = mock
        result = await _tavily_search("hello", 3, "general")
    assert "error" in result
    assert "exhausted" in result["error"]


@pytest.mark.asyncio
async def test_tavily_legacy_single_key_var(monkeypatch):
    """TAVILY_API_KEY (singular, legacy) is also accepted."""
    monkeypatch.delenv("TAVILY_API_KEYS", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "legacy-only")
    ok_resp = _mk_response(200, {"answer": "via legacy", "results": []})
    with patch("longai_mcps.web_search.server.httpx.AsyncClient") as MockClient:
        mock = AsyncMock()
        mock.post = AsyncMock(return_value=ok_resp)
        MockClient.return_value.__aenter__.return_value = mock
        result = await _tavily_search("q", 3, "general")
    assert result["answer"] == "via legacy"
