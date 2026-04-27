"""Pre-LLM enrichment — fetches market data when the router detects a symbol."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from longai import enrichment
from longai.mcp_client import UnknownTool
from longai.router import RouteHints


@pytest.fixture
def fake_mcp():
    m = MagicMock()
    m.call = AsyncMock()
    return m


@pytest.mark.asyncio
async def test_no_symbol_returns_none(fake_mcp):
    out = await enrichment.enrich(RouteHints(), fake_mcp)
    assert out is None
    fake_mcp.call.assert_not_called()


@pytest.mark.asyncio
async def test_symbol_fetches_and_formats(fake_mcp):
    fake_mcp.call.return_value = json.dumps({
        "current_price_usd": 2275.65,
        "price_change_24h_pct": 2.1,
    })
    out = await enrichment.enrich(RouteHints(symbol="ETH"), fake_mcp)
    assert out is not None
    assert "ETH: $2,275.65" in out
    assert "+2.10% 24h" in out
    assert "pre-fetched" in out  # the no-recall directive
    fake_mcp.call.assert_awaited_once_with("coingecko_token_info", {"symbol": "ETH"})


@pytest.mark.asyncio
async def test_negative_change_formats_with_minus(fake_mcp):
    fake_mcp.call.return_value = json.dumps({
        "current_price_usd": 2200.0,
        "price_change_24h_pct": -1.5,
    })
    out = await enrichment.enrich(RouteHints(symbol="ETH"), fake_mcp)
    assert "-1.50% 24h" in out


@pytest.mark.asyncio
async def test_missing_tool_returns_none(fake_mcp):
    fake_mcp.call.side_effect = UnknownTool("not registered")
    out = await enrichment.enrich(RouteHints(symbol="ETH"), fake_mcp)
    assert out is None


@pytest.mark.asyncio
async def test_tool_error_response_returns_none(fake_mcp):
    fake_mcp.call.return_value = json.dumps({"error": "rate limited"})
    out = await enrichment.enrich(RouteHints(symbol="ETH"), fake_mcp)
    assert out is None


@pytest.mark.asyncio
async def test_malformed_response_returns_none(fake_mcp):
    fake_mcp.call.return_value = "not json"
    out = await enrichment.enrich(RouteHints(symbol="ETH"), fake_mcp)
    assert out is None


@pytest.mark.asyncio
async def test_no_price_field_returns_none(fake_mcp):
    fake_mcp.call.return_value = json.dumps({"name": "Ethereum"})  # no price
    out = await enrichment.enrich(RouteHints(symbol="ETH"), fake_mcp)
    assert out is None


@pytest.mark.asyncio
async def test_nested_market_data_shape_is_handled(fake_mcp):
    """CoinGecko sometimes returns price under market_data.current_price.usd."""
    fake_mcp.call.return_value = json.dumps({
        "market_data": {
            "current_price": {"usd": 2275.65},
            "price_change_percentage_24h": 2.1,
        },
    })
    out = await enrichment.enrich(RouteHints(symbol="ETH"), fake_mcp)
    assert out is not None
    assert "ETH: $2,275.65" in out


# ── attach() ───────────────────────────────────────────────────────────


def test_attach_with_block():
    out = enrichment.attach("what's ETH trading at", "[market] ETH: $2,275")
    assert out.startswith("[market] ETH: $2,275")
    assert out.endswith("what's ETH trading at")
    assert "\n\n" in out


def test_attach_without_block_returns_unchanged():
    assert enrichment.attach("hello", None) == "hello"
