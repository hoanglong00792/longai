"""Slash-command bypass — direct compute, no LLM, no spend."""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from longai import fast_commands
from longai.mcp_client import UnknownTool


@pytest.fixture
def fake_mcp():
    m = MagicMock()
    m.call = AsyncMock()
    return m


@pytest.fixture
def fake_persistence():
    p = MagicMock()
    p.spend_today = MagicMock(return_value=0.0123)
    return p


# ── is_fast_command ────────────────────────────────────────────────────


@pytest.mark.parametrize("msg,expected", [
    ("/price ETH", True),
    ("/ta BTC", True),
    ("/caps", True),
    ("/help", True),
    ("/price", True),  # bare command (no arg)
    ("price ETH", False),  # missing slash
    ("hello /price", False),  # not at start
    ("", False),
])
def test_is_fast_command(msg, expected):
    assert fast_commands.is_fast_command(msg) == expected


# ── dispatch — non-matching returns None ───────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_returns_none_for_non_command(fake_mcp, fake_persistence):
    out = await fast_commands.dispatch(
        "what's ETH trading at", mcp=fake_mcp, persistence=fake_persistence,
    )
    assert out is None


# ── /price ─────────────────────────────────────────────────────────────


@pytest.fixture
def patch_simple_price(monkeypatch):
    def _swap(fake):
        monkeypatch.setattr(
            "longai.fast_commands.coingecko_simple_price", fake, raising=True,
        )
    return _swap


@pytest.mark.asyncio
async def test_price_happy_path(fake_mcp, fake_persistence, patch_simple_price):
    async def fake(symbol):
        return {"symbol": "ETH", "coin_id": "ethereum",
                "price_usd": 2275.65, "change_24h_pct": -1.2}
    patch_simple_price(fake)
    out = await fast_commands.dispatch(
        "/price ETH", mcp=fake_mcp, persistence=fake_persistence,
    )
    assert out is not None
    assert out.error is None
    assert "ETH: $2,275.65" in out.text
    assert "-1.20% 24h" in out.text
    fake_mcp.call.assert_not_called()  # /price uses HTTP, not MCP


@pytest.mark.asyncio
async def test_price_no_arg_shows_usage(fake_mcp, fake_persistence):
    out = await fast_commands.dispatch(
        "/price", mcp=fake_mcp, persistence=fake_persistence,
    )
    assert "Usage" in out.text
    fake_mcp.call.assert_not_called()


@pytest.mark.asyncio
async def test_price_unknown_symbol_returns_helpful_error(fake_mcp, fake_persistence):
    out = await fast_commands.dispatch(
        "/price XYZQQQ", mcp=fake_mcp, persistence=fake_persistence,
    )
    assert out.error == "unknown_symbol"
    assert "contract" in out.text.lower()  # hint to use a contract address


@pytest.mark.asyncio
async def test_price_fetch_failure_returns_error(fake_mcp, fake_persistence, patch_simple_price):
    async def fake(symbol):
        return None
    patch_simple_price(fake)
    out = await fast_commands.dispatch(
        "/price ETH", mcp=fake_mcp, persistence=fake_persistence,
    )
    assert out.error == "fetch_failed"


@pytest.mark.asyncio
async def test_price_uppercases_symbol(fake_mcp, fake_persistence, patch_simple_price):
    captured: list[str] = []
    async def fake(symbol):
        captured.append(symbol)
        return {"symbol": "ETH", "coin_id": "ethereum",
                "price_usd": 1.0, "change_24h_pct": 0.0}
    patch_simple_price(fake)
    await fast_commands.dispatch(
        "/price eth", mcp=fake_mcp, persistence=fake_persistence,
    )
    assert captured == ["ETH"]


# ── /ta ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ta_formats_full_report(fake_mcp, fake_persistence):
    fake_mcp.call.return_value = json.dumps({
        "symbol": "BTC",
        "current_price_usd": 67890.0,
        "market_cap_usd": 1.34e12,
        "liquidity_usd": 5e9,
        "volume_24h_usd": 3.2e10,
        "price_change_24h_pct": 1.5,
    })
    out = await fast_commands.dispatch(
        "/ta BTC", mcp=fake_mcp, persistence=fake_persistence,
    )
    assert "TA: BTC" in out.text
    assert "Price: $67,890.00" in out.text
    assert "Market cap" in out.text
    assert "Liquidity" in out.text
    assert "+1.50%" in out.text


@pytest.mark.asyncio
async def test_ta_handles_partial_data(fake_mcp, fake_persistence):
    fake_mcp.call.return_value = json.dumps({"symbol": "FOO"})
    out = await fast_commands.dispatch(
        "/ta FOO", mcp=fake_mcp, persistence=fake_persistence,
    )
    assert "TA: FOO" in out.text
    assert "no analysis fields" in out.text


# ── /caps ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_caps_reads_persistence_no_mcp(fake_mcp, fake_persistence):
    out = await fast_commands.dispatch(
        "/caps", mcp=fake_mcp, persistence=fake_persistence,
    )
    assert "$0.0123" in out.text
    fake_mcp.call.assert_not_called()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fake_persistence.spend_today.assert_called_once_with(today)


# ── /help ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_help_lists_all_commands(fake_mcp, fake_persistence):
    out = await fast_commands.dispatch(
        "/help", mcp=fake_mcp, persistence=fake_persistence,
    )
    for cmd in ("/price", "/ta", "/caps", "/help"):
        assert cmd in out.text
