"""I3, I4 — the only OpenRouter caller. Caps + fallback + cooldown."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from longai.budget_guard import (
    AllModelsCooled,
    BudgetGuard,
    CallTimeout,
    Unauthorized,
)
from longai.config import BudgetCaps
from longai.persistence import BudgetExceeded, Persistence


@pytest.fixture
def caps_tiny():
    return BudgetCaps(
        global_daily_usd=1.0, per_user_daily_usd=0.5,
        per_call_max_usd=0.10, per_call_wall_clock_s=5, per_call_max_turns=3,
    )


@pytest.fixture
def persistence(tmp_db_path):
    p = Persistence(tmp_db_path)
    p.init()
    yield p
    p.close()


def _mk_response(content: str, p_tokens: int = 50, c_tokens: int = 30, model: str = "x/y"):
    """Build a fake OpenAI-shape response object."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = None
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = MagicMock(prompt_tokens=p_tokens, completion_tokens=c_tokens)
    resp.model = model
    return resp


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest.mark.asyncio
async def test_happy_path_debits_and_returns(caps_tiny, persistence, monkeypatch):
    g = BudgetGuard(
        api_key="sk", base_url="x", models=["m1"],
        caps=caps_tiny, persistence=persistence,
        prices={"m1": (1.0, 2.0)},  # $1/M input, $2/M output
    )
    fake_call = AsyncMock(return_value=_mk_response("ok", 100, 50, "m1"))
    monkeypatch.setattr(g, "_raw_call", fake_call)

    res = await g.chat(chat_id=1, messages=[{"role": "user", "content": "hi"}], tools=None)
    assert res.text == "ok"
    assert res.model_used == "m1"
    # Cost = 100/1e6 * 1 + 50/1e6 * 2 = 0.0001 + 0.0001 = 0.0002
    assert res.spend_usd == pytest.approx(0.0002, rel=0.01)
    # Persistence debit happened
    assert persistence.spend_today(_today()) > 0


@pytest.mark.asyncio
async def test_per_call_cap_aborts_after_oversized_call(caps_tiny, persistence, monkeypatch):
    """If a single call's cost exceeds per_call_max_usd, raise after debit attempt."""
    caps_tiny.per_call_max_usd = 0.00001  # tiny
    g = BudgetGuard(
        api_key="sk", base_url="x", models=["m1"],
        caps=caps_tiny, persistence=persistence, prices={"m1": (10.0, 10.0)},
    )
    fake_call = AsyncMock(return_value=_mk_response("ok", 1000, 500, "m1"))
    monkeypatch.setattr(g, "_raw_call", fake_call)

    with pytest.raises(BudgetExceeded) as exc:
        await g.chat(chat_id=1, messages=[{"role": "user", "content": "x"}], tools=None)
    assert "per_call" in exc.value.scope or "per_user" in exc.value.scope or "global" in exc.value.scope


@pytest.mark.asyncio
async def test_unauthorized_does_not_fall_back(caps_tiny, persistence, monkeypatch):
    """F7 — HTTP 401 is fatal, no fallback chain attempted."""
    g = BudgetGuard(
        api_key="sk", base_url="x", models=["m1", "m2"],
        caps=caps_tiny, persistence=persistence, prices={"m1": (1, 1), "m2": (1, 1)},
    )
    monkeypatch.setattr(g, "_raw_call", AsyncMock(side_effect=Unauthorized("bad key")))
    with pytest.raises(Unauthorized):
        await g.chat(chat_id=1, messages=[{"role": "user", "content": "x"}], tools=None)


@pytest.mark.asyncio
async def test_fallback_chain_skips_cooled(caps_tiny, persistence, monkeypatch):
    """F8/F12 — cool m1, expect m2 used."""
    persistence.set_cooldown("m1", until_ts=10**12)  # far future
    g = BudgetGuard(
        api_key="sk", base_url="x", models=["m1", "m2"],
        caps=caps_tiny, persistence=persistence, prices={"m1": (1, 1), "m2": (1, 1)},
    )
    monkeypatch.setattr(g, "_raw_call", AsyncMock(return_value=_mk_response("ok", model="m2")))
    res = await g.chat(chat_id=1, messages=[{"role": "user", "content": "x"}], tools=None)
    assert res.model_used == "m2"


@pytest.mark.asyncio
async def test_all_models_cooled_raises(caps_tiny, persistence):
    persistence.set_cooldown("m1", until_ts=10**12)
    g = BudgetGuard(
        api_key="sk", base_url="x", models=["m1"],
        caps=caps_tiny, persistence=persistence, prices={"m1": (1, 1)},
    )
    with pytest.raises(AllModelsCooled):
        await g.chat(chat_id=1, messages=[{"role": "user", "content": "x"}], tools=None)
