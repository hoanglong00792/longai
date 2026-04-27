"""I8 — telegram_out enforces destination chat_id whitelist at tool layer."""
import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from longai_mcps.telegram_out.server import _send_message_impl


@pytest.mark.asyncio
async def test_unauthorized_chat_id_rejected(monkeypatch):
    monkeypatch.setenv("LONGAI_OUTBOUND_WHITELIST", "100,200")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    fake_post = AsyncMock()
    with patch("longai_mcps.telegram_out.server.httpx.AsyncClient.post", fake_post):
        result = await _send_message_impl(chat_id=999, text="leak")
    assert "error" in result
    assert "whitelist" in result["error"].lower()
    fake_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_chat_id_succeeds(monkeypatch):
    monkeypatch.setenv("LONGAI_OUTBOUND_WHITELIST", "100,200")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    fake_resp = AsyncMock()
    fake_resp.return_value.json = AsyncMock(return_value={"ok": True})
    fake_resp.return_value.raise_for_status = lambda: None
    with patch("longai_mcps.telegram_out.server.httpx.AsyncClient.post", fake_resp):
        result = await _send_message_impl(chat_id=100, text="ok")
    assert "error" not in result


@pytest.mark.asyncio
async def test_empty_whitelist_rejects_all(monkeypatch):
    monkeypatch.setenv("LONGAI_OUTBOUND_WHITELIST", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    fake_post = AsyncMock()
    with patch("longai_mcps.telegram_out.server.httpx.AsyncClient.post", fake_post):
        result = await _send_message_impl(chat_id=999, text="leak")
    assert "error" in result
    fake_post.assert_not_awaited()
