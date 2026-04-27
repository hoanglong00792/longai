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


@pytest.fixture
def patch_simple_price(monkeypatch):
    """Monkeypatch ``prices.coingecko_simple_price`` and the rebound symbol in
    ``enrichment``. The fixture yields a callable that swaps in a fake."""
    def _swap(fake):
        monkeypatch.setattr(
            "longai.enrichment.coingecko_simple_price", fake,
            raising=True,
        )
    return _swap


@pytest.mark.asyncio
async def test_no_symbol_returns_none(fake_mcp):
    out = await enrichment.enrich(RouteHints(), fake_mcp)
    assert out is None
    fake_mcp.call.assert_not_called()


@pytest.mark.asyncio
async def test_symbol_fetches_and_formats(fake_mcp, patch_simple_price):
    async def fake(symbol):
        return {"symbol": "ETH", "coin_id": "ethereum",
                "price_usd": 2275.65, "change_24h_pct": 2.1}
    patch_simple_price(fake)
    out = await enrichment.enrich(RouteHints(symbol="ETH"), fake_mcp)
    assert out is not None
    assert "ETH: $2,275.65" in out
    assert "+2.10% 24h" in out
    assert "Pre-fetched" in out


@pytest.mark.asyncio
async def test_negative_change_formats_with_minus(fake_mcp, patch_simple_price):
    async def fake(symbol):
        return {"symbol": "ETH", "coin_id": "ethereum",
                "price_usd": 2200.0, "change_24h_pct": -1.5}
    patch_simple_price(fake)
    out = await enrichment.enrich(RouteHints(symbol="ETH"), fake_mcp)
    assert "-1.50% 24h" in out


@pytest.mark.asyncio
async def test_unknown_symbol_returns_none(fake_mcp, patch_simple_price):
    """coingecko_simple_price returns None for symbols not in COINGECKO_IDS."""
    async def fake(symbol):
        return None
    patch_simple_price(fake)
    out = await enrichment.enrich(RouteHints(symbol="NOTASYMBOL"), fake_mcp)
    assert out is None


@pytest.mark.asyncio
async def test_no_change_field_still_renders(fake_mcp, patch_simple_price):
    async def fake(symbol):
        return {"symbol": "ETH", "coin_id": "ethereum",
                "price_usd": 2275.65, "change_24h_pct": None}
    patch_simple_price(fake)
    out = await enrichment.enrich(RouteHints(symbol="ETH"), fake_mcp)
    assert out is not None
    assert "ETH: $2,275.65" in out
    assert "24h" not in out  # change suffix omitted


# ── attach() ───────────────────────────────────────────────────────────


def test_attach_with_block():
    out = enrichment.attach("what's ETH trading at", "[market] ETH: $2,275")
    assert out.startswith("[market] ETH: $2,275")
    assert out.endswith("what's ETH trading at")
    assert "\n\n" in out


def test_attach_without_block_returns_unchanged():
    assert enrichment.attach("hello", None) == "hello"


# ── PR E: contract enrichment ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_contract_fetches_and_formats(fake_mcp):
    fake_mcp.call.return_value = json.dumps({
        "pairs": [
            {
                "baseToken": {"symbol": "PEPE", "name": "Pepe"},
                "priceUsd": "0.0000018",
                "liquidity": {"usd": 5_200_000},
                "volume": {"h24": 12_000_000},
            }
        ]
    })
    hints = RouteHints(
        contract="0x6982508145454ce325ddbe47a25d4ec3d2311933",
        chain="ethereum",
    )
    out = await enrichment.enrich(hints, fake_mcp)
    assert out is not None
    assert "PEPE" in out
    assert "0x6982" in out
    assert "ethereum" in out
    assert "liq $5,200,000" in out
    assert "vol24h $12,000,000" in out


@pytest.mark.asyncio
async def test_contract_picks_most_liquid_pair(fake_mcp):
    fake_mcp.call.return_value = json.dumps({
        "pairs": [
            {"baseToken": {"symbol": "X"}, "liquidity": {"usd": 100}},
            {"baseToken": {"symbol": "X"}, "liquidity": {"usd": 9_999_999}, "priceUsd": "1.0"},
            {"baseToken": {"symbol": "X"}, "liquidity": {"usd": 50}},
        ]
    })
    out = await enrichment.enrich(
        RouteHints(contract="0x" + "1" * 40, chain="base"), fake_mcp,
    )
    assert "liq $9,999,999" in out


@pytest.mark.asyncio
async def test_contract_no_pairs_returns_none(fake_mcp):
    fake_mcp.call.return_value = json.dumps({"pairs": []})
    out = await enrichment.enrich(
        RouteHints(contract="0x" + "0" * 40, chain="ethereum"), fake_mcp,
    )
    assert out is None


# ── PR E: URL enrichment ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_url_fetches_and_truncates(fake_mcp):
    long_text = "alpha " * 1000  # ~6KB, well over the 1500 cap
    fake_mcp.call.return_value = json.dumps({"text": long_text})
    out = await enrichment.enrich(
        RouteHints(urls=["https://example.com/article"]), fake_mcp,
    )
    assert out is not None
    assert "[https://example.com/article]" in out
    assert "[…truncated]" in out
    # Body length capped at ~1500 chars (plus the truncation marker)
    assert len(out) < 2000


@pytest.mark.asyncio
async def test_url_short_body_not_truncated(fake_mcp):
    fake_mcp.call.return_value = json.dumps({"text": "short article body"})
    out = await enrichment.enrich(
        RouteHints(urls=["https://example.com/x"]), fake_mcp,
    )
    assert out is not None
    assert "[…truncated]" not in out
    assert "short article body" in out


@pytest.mark.asyncio
async def test_url_caps_at_three(fake_mcp):
    fake_mcp.call.return_value = json.dumps({"text": "body"})
    urls = [f"https://example.com/{i}" for i in range(10)]
    out = await enrichment.enrich(RouteHints(urls=urls), fake_mcp)
    assert out is not None
    # Only first 3 URLs should be fetched
    assert fake_mcp.call.await_count == 3


# ── PR E: parallel enrichment ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_enrichments_run_concurrently(fake_mcp, patch_simple_price):
    """Symbol + contract + URL all detected → all fetched in one gather.

    Symbol path goes through prices.coingecko_simple_price (HTTP, not MCP).
    Contract and URL paths go through MCP.
    """
    async def fake_price(symbol):
        return {"symbol": "ETH", "coin_id": "ethereum",
                "price_usd": 1.0, "change_24h_pct": 0.0}
    patch_simple_price(fake_price)

    async def router_call(name, args):
        if name == "dexscreener_pairs":
            return json.dumps({"pairs": [{"baseToken": {"symbol": "X"}}]})
        if name == "fetch_url":
            return json.dumps({"text": "article body"})
        return json.dumps({"error": "unknown"})

    fake_mcp.call.side_effect = router_call
    hints = RouteHints(
        symbol="ETH",
        contract="0x" + "a" * 40,
        chain="ethereum",
        urls=["https://example.com"],
    )
    out = await enrichment.enrich(hints, fake_mcp)
    assert out is not None
    assert "ETH" in out
    assert "X" in out  # contract symbol
    assert "article body" in out
    # MCP called for contract + URL only — market goes via direct HTTP
    assert fake_mcp.call.await_count == 2
