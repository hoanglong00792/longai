"""Loop.run: stuck-loop trap, error-as-content, sanitization integration."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from longai.budget_guard import ChatResult
from longai.loop import Loop, LoopResult
from longai.persistence import BudgetExceeded


@pytest.fixture
def fake_guard():
    g = MagicMock()
    g.chat = AsyncMock()
    return g


@pytest.fixture
def fake_mcp():
    m = MagicMock()
    m.tools = MagicMock(return_value=[])
    m.call = AsyncMock()
    return m


def _result(text="", tool_calls=None, model="m1", spend=0.001, p=10, c=5):
    return ChatResult(
        text=text, tool_calls=tool_calls, prompt_tokens=p, completion_tokens=c,
        spend_usd=spend, model_used=model,
    )


@pytest.mark.asyncio
async def test_final_answer_stops_loop(fake_guard, fake_mcp):
    fake_guard.chat.return_value = _result(text="here is the answer")
    loop = Loop(guard=fake_guard, mcp=fake_mcp, max_turns=5)
    res = await loop.run(chat_id=1, system_prompt="sys", user_message="hi", history=[])
    assert res.text == "here is the answer"
    assert res.stopped == "final"
    assert res.turns == 1


@pytest.mark.asyncio
async def test_max_turns_stops_loop(fake_guard, fake_mcp):
    """Always emit a tool call, never a final — should stop at max_turns."""
    fake_guard.chat.return_value = _result(
        text="", tool_calls=[{"id": "t", "name": "echo", "arguments": '{"text":"x"}'}],
    )
    fake_mcp.call.return_value = "ok"
    loop = Loop(guard=fake_guard, mcp=fake_mcp, max_turns=3)
    res = await loop.run(chat_id=1, system_prompt="sys", user_message="hi", history=[])
    assert res.stopped == "max_turns"
    assert res.turns == 3


@pytest.mark.asyncio
async def test_stuck_loop_trap_fires_at_three(fake_guard, fake_mcp):
    """F6 — three identical (name, args) hashes → bail."""
    fake_guard.chat.return_value = _result(
        text="", tool_calls=[{"id": "t", "name": "echo", "arguments": '{"text":"same"}'}],
    )
    fake_mcp.call.return_value = "ok"
    loop = Loop(guard=fake_guard, mcp=fake_mcp, max_turns=10)
    res = await loop.run(chat_id=1, system_prompt="sys", user_message="hi", history=[])
    assert res.stopped == "stuck_loop"
    assert res.turns <= 3  # bails on/around turn 3


@pytest.mark.asyncio
async def test_error_as_content_recovers(fake_guard, fake_mcp):
    """F14 — tool crash becomes content, model continues."""
    # Turn 1: tool call. Turn 2: final.
    fake_guard.chat.side_effect = [
        _result(tool_calls=[{"id": "t", "name": "echo", "arguments": "{}"}]),
        _result(text="ok done"),
    ]
    fake_mcp.call.return_value = json.dumps({"error": "tool crashed: echo"})
    loop = Loop(guard=fake_guard, mcp=fake_mcp, max_turns=5)
    res = await loop.run(chat_id=1, system_prompt="sys", user_message="hi", history=[])
    assert res.text == "ok done"
    assert res.stopped == "final"
    assert res.turns == 2


@pytest.mark.asyncio
async def test_budget_exceeded_returns_envelope(fake_guard, fake_mcp):
    fake_guard.chat.side_effect = BudgetExceeded("per_user", 0.30, 0.25)
    loop = Loop(guard=fake_guard, mcp=fake_mcp, max_turns=5)
    res = await loop.run(chat_id=1, system_prompt="sys", user_message="hi", history=[])
    assert res.stopped == "budget"
    assert "per_user" in (res.error or "")


@pytest.mark.asyncio
async def test_sanitize_tool_output_runs_on_results(fake_guard, fake_mcp):
    """I11 — tool outputs are sanitized before going back into context."""
    fake_guard.chat.side_effect = [
        _result(tool_calls=[{"id": "t", "name": "echo", "arguments": "{}"}]),
        _result(text="done"),
    ]
    # Tool returns a wallet address — must be masked
    fake_mcp.call.return_value = "addr=0x6131B5fae19EA4f9D964eAc0408E4408b66337b5"
    loop = Loop(guard=fake_guard, mcp=fake_mcp, max_turns=5)
    res = await loop.run(chat_id=1, system_prompt="sys", user_message="hi", history=[])
    # Inspect what was passed to the second chat call (history must be sanitized)
    call_args_2 = fake_guard.chat.call_args_list[1]
    sent_messages = call_args_2.kwargs["messages"]
    raw_addr = "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5"
    assert all(raw_addr not in (m.get("content") or "") for m in sent_messages)
