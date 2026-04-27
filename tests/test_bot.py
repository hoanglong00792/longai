# tests/test_bot.py
"""Telegram surface — whitelist enforcement, sanitizer wiring."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from longai.bot import _is_whitelisted, _sanitize_reply


def test_whitelist_check():
    assert _is_whitelisted(123, [123, 456]) is True
    assert _is_whitelisted(999, [123, 456]) is False
    assert _is_whitelisted(123, []) is False


def test_sanitize_reply_masks_wallets():
    text = "addr 0x6131B5fae19EA4f9D964eAc0408E4408b66337b5"
    assert "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5" not in _sanitize_reply(text)
