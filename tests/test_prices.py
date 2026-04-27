"""Tests for the symbol→price helper used by enrichment + /price."""
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from longai.prices import (
    COINGECKO_IDS,
    coingecko_simple_price,
    format_price_line,
)


@pytest.fixture
def fake_client():
    """An ``httpx.AsyncClient`` stand-in. .get returns a mocked Response."""
    c = MagicMock()
    c.get = AsyncMock()
    return c


def _resp(status: int, payload):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=payload)
    return r


@pytest.mark.asyncio
async def test_returns_none_for_unknown_symbol(fake_client):
    out = await coingecko_simple_price("NOTASYMBOL", client=fake_client)
    assert out is None
    fake_client.get.assert_not_called()


@pytest.mark.asyncio
async def test_known_symbol_fetches_and_normalizes(fake_client):
    fake_client.get.return_value = _resp(200, {
        "ethereum": {"usd": 2275.65, "usd_24h_change": 2.1},
    })
    out = await coingecko_simple_price("ETH", client=fake_client)
    assert out == {
        "symbol": "ETH", "coin_id": "ethereum",
        "price_usd": 2275.65, "change_24h_pct": 2.1,
    }


@pytest.mark.asyncio
async def test_lowercase_input_normalized(fake_client):
    fake_client.get.return_value = _resp(200, {
        "bitcoin": {"usd": 67890.0, "usd_24h_change": 1.5},
    })
    out = await coingecko_simple_price("btc", client=fake_client)
    assert out["symbol"] == "BTC"
    assert out["coin_id"] == "bitcoin"


@pytest.mark.asyncio
async def test_404_returns_none(fake_client):
    fake_client.get.return_value = _resp(404, {})
    out = await coingecko_simple_price("ETH", client=fake_client)
    assert out is None


@pytest.mark.asyncio
async def test_network_error_returns_none(fake_client):
    fake_client.get.side_effect = httpx.ConnectError("DNS fail")
    out = await coingecko_simple_price("ETH", client=fake_client)
    assert out is None


@pytest.mark.asyncio
async def test_missing_price_field_returns_none(fake_client):
    fake_client.get.return_value = _resp(200, {"ethereum": {}})
    out = await coingecko_simple_price("ETH", client=fake_client)
    assert out is None


@pytest.mark.asyncio
async def test_missing_change_field_yields_none_change(fake_client):
    """Price-only response is still useful — change just missing."""
    fake_client.get.return_value = _resp(200, {
        "ethereum": {"usd": 2275.65},
    })
    out = await coingecko_simple_price("ETH", client=fake_client)
    assert out["price_usd"] == 2275.65
    assert out["change_24h_pct"] is None


def test_format_price_line_with_change():
    line = format_price_line({
        "symbol": "ETH", "price_usd": 2275.65, "change_24h_pct": 2.1,
    })
    assert line == "ETH: $2,275.65 (+2.10% 24h)"


def test_format_price_line_negative_change():
    line = format_price_line({
        "symbol": "BTC", "price_usd": 67890.0, "change_24h_pct": -1.5,
    })
    assert "-1.50% 24h" in line


def test_format_price_line_no_change():
    line = format_price_line({
        "symbol": "ETH", "price_usd": 2275.65, "change_24h_pct": None,
    })
    assert line == "ETH: $2,275.65"
    assert "24h" not in line


def test_id_map_covers_curated_assets():
    """Every symbol in router.ASSET_MAP should have a CoinGecko id mapped."""
    from longai.router import ASSET_MAP
    missing = ASSET_MAP - set(COINGECKO_IDS)
    assert not missing, f"Symbols in ASSET_MAP missing from COINGECKO_IDS: {missing}"
