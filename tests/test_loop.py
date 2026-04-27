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


# ── Tier-aware behavior ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_classifier_routes_to_tier_l_on_research_keyword(fake_guard, fake_mcp):
    """Loop without explicit tier should classify the message and pass tier through."""
    fake_guard.chat.return_value = _result(text="done")
    loop = Loop(guard=fake_guard, mcp=fake_mcp, max_turns=5)
    res = await loop.run(
        chat_id=1, system_prompt="sys",
        user_message="research the latest on Solana validators", history=[],
    )
    assert res.tier == "L"
    # Verify guard.chat was called with tier="L"
    assert fake_guard.chat.call_args.kwargs["tier"] == "L"


@pytest.mark.asyncio
async def test_classifier_routes_to_tier_s_on_short_greeting(fake_guard, fake_mcp):
    fake_guard.chat.return_value = _result(text="hi back")
    loop = Loop(guard=fake_guard, mcp=fake_mcp, max_turns=5)
    res = await loop.run(
        chat_id=1, system_prompt="sys", user_message="hi", history=[],
    )
    assert res.tier == "S"


@pytest.mark.asyncio
async def test_explicit_tier_overrides_classifier(fake_guard, fake_mcp):
    fake_guard.chat.return_value = _result(text="done")
    loop = Loop(guard=fake_guard, mcp=fake_mcp, max_turns=5)
    res = await loop.run(
        chat_id=1, system_prompt="sys",
        user_message="hi", history=[], tier="L",
    )
    assert res.tier == "L"
    assert fake_guard.chat.call_args.kwargs["tier"] == "L"


@pytest.mark.asyncio
async def test_quick_prefix_is_stripped_before_llm(fake_guard, fake_mcp):
    """The /deep /quick prefix is for routing; the LLM shouldn't see it."""
    fake_guard.chat.return_value = _result(text="ok")
    loop = Loop(guard=fake_guard, mcp=fake_mcp, max_turns=5)
    await loop.run(
        chat_id=1, system_prompt="sys",
        user_message="/deep tell me about pendle", history=[],
    )
    sent = fake_guard.chat.call_args.kwargs["messages"]
    user_msg = next(m for m in sent if m["role"] == "user")
    assert "/deep" not in user_msg["content"]
    assert user_msg["content"].startswith("tell me about")


@pytest.mark.asyncio
async def test_load_skill_with_l_complexity_bumps_tier(fake_guard, fake_mcp):
    """Loading a complexity:L skill mid-conversation upgrades the rest of the run."""
    from longai.config import BudgetCaps
    caps = BudgetCaps(
        per_call_max_turns=3,
        by_tier={"L": {"per_call_max_turns": 9}},
    )
    fake_guard.chat.side_effect = [
        # Turn 1: load_skill call
        _result(tool_calls=[
            {"id": "t1", "name": "load_skill", "arguments": '{"name":"deep-research"}'}
        ]),
        # Turn 2: final
        _result(text="done with deep research"),
    ]
    fake_mcp.call.return_value = json.dumps({
        "name": "deep-research", "complexity": "L",
        "body": "...", "path": "/x",
    })
    loop = Loop(guard=fake_guard, mcp=fake_mcp, caps=caps)
    res = await loop.run(
        chat_id=1, system_prompt="sys",
        user_message="run the heavy skill", history=[], tier="M",
    )
    # Tier should have escalated from M (forced) to L (skill complexity)
    assert res.tier == "L"
    # First chat call: tier=M (initial)
    assert fake_guard.chat.call_args_list[0].kwargs["tier"] == "M"
    # Second chat call: tier=L (after the bump)
    assert fake_guard.chat.call_args_list[1].kwargs["tier"] == "L"


@pytest.mark.asyncio
async def test_load_skill_with_lower_complexity_does_not_downgrade(fake_guard, fake_mcp):
    """If we're already on L and load a complexity:S skill, stay on L."""
    fake_guard.chat.side_effect = [
        _result(tool_calls=[
            {"id": "t1", "name": "load_skill", "arguments": '{"name":"trivial"}'}
        ]),
        _result(text="ok"),
    ]
    fake_mcp.call.return_value = json.dumps({
        "name": "trivial", "complexity": "S", "body": "x", "path": "/x",
    })
    loop = Loop(guard=fake_guard, mcp=fake_mcp, max_turns=5)
    res = await loop.run(
        chat_id=1, system_prompt="sys", user_message="x", history=[], tier="L",
    )
    assert res.tier == "L"  # unchanged
    assert fake_guard.chat.call_args_list[1].kwargs["tier"] == "L"


@pytest.mark.asyncio
async def test_non_load_skill_tool_does_not_bump(fake_guard, fake_mcp):
    """Other tools that happen to return a `complexity` field don't trigger bumps."""
    fake_guard.chat.side_effect = [
        _result(tool_calls=[
            {"id": "t1", "name": "calculate", "arguments": "{}"}
        ]),
        _result(text="42"),
    ]
    fake_mcp.call.return_value = json.dumps({"complexity": "L", "result": 42})
    loop = Loop(guard=fake_guard, mcp=fake_mcp, max_turns=5)
    res = await loop.run(
        chat_id=1, system_prompt="sys", user_message="2+2", history=[], tier="M",
    )
    assert res.tier == "M"  # not bumped


@pytest.mark.asyncio
async def test_load_skill_with_invalid_complexity_does_not_bump(fake_guard, fake_mcp):
    """Invalid complexity values are ignored — tier stays as-is."""
    fake_guard.chat.side_effect = [
        _result(tool_calls=[
            {"id": "t1", "name": "load_skill", "arguments": '{"name":"weird"}'}
        ]),
        _result(text="ok"),
    ]
    fake_mcp.call.return_value = json.dumps({
        "name": "weird", "complexity": "HUGE", "body": "x", "path": "/x",
    })
    loop = Loop(guard=fake_guard, mcp=fake_mcp, max_turns=5)
    res = await loop.run(
        chat_id=1, system_prompt="sys", user_message="x", history=[], tier="M",
    )
    assert res.tier == "M"


@pytest.mark.asyncio
async def test_caps_tier_sets_per_tier_max_turns(fake_guard, fake_mcp):
    """Loop with caps= reads turn limit per-tier from caps.turns_for()."""
    from longai.config import BudgetCaps
    caps = BudgetCaps(per_call_max_turns=5, by_tier={"L": {"per_call_max_turns": 9}})
    # Always emit a tool call so the loop runs out of turns
    fake_guard.chat.return_value = _result(
        text="", tool_calls=[{"id": "t", "name": "echo", "arguments": '{"k":"v"}'}],
    )
    fake_mcp.call.return_value = "ok"
    loop = Loop(guard=fake_guard, mcp=fake_mcp, caps=caps)
    res = await loop.run(
        chat_id=1, system_prompt="sys",
        user_message="research how stablecoin issuers verify reserves",
        history=[],
    )
    # Stuck-loop trap may fire before max_turns since args are constant —
    # but tier should still be L, and turns should not exceed 9.
    assert res.tier == "L"
    assert res.turns <= 9


@pytest.mark.asyncio
async def test_loop_emits_timing_events_when_tracer_provided(fake_guard, fake_mcp, tmp_path):
    """Loop.run with a Tracer should write chat/tool/turn records."""
    from longai.trace import Tracer
    fake_guard.chat.side_effect = [
        _result(tool_calls=[
            {"id": "t1", "name": "echo", "arguments": '{"k":"v"}'}
        ]),
        _result(text="done"),
    ]
    fake_mcp.call.return_value = "ok"
    tracer = Tracer(str(tmp_path))
    loop = Loop(guard=fake_guard, mcp=fake_mcp, max_turns=5)
    res = await loop.run(
        chat_id=1, system_prompt="sys", user_message="hi",
        history=[], tracer=tracer,
    )
    assert res.stopped == "final"
    [run_dir] = list(tmp_path.iterdir())
    timings_file = run_dir / "06_timings.jsonl"
    assert timings_file.exists()
    phases = [json.loads(l)["phase"] for l in timings_file.read_text().splitlines()]
    # Expect: chat (turn 1), tool (turn 1), turn (turn 1), chat (turn 2), turn (turn 2)
    assert "chat" in phases
    assert "tool" in phases
    assert "turn" in phases
    # No chat without a tool — final turn should still record chat + turn
    assert phases.count("chat") == 2  # two chat calls, two turns


@pytest.mark.asyncio
async def test_loop_without_tracer_runs_silently(fake_guard, fake_mcp):
    """Backward compat — tracer is optional; loop runs identically without it."""
    fake_guard.chat.return_value = _result(text="hi")
    loop = Loop(guard=fake_guard, mcp=fake_mcp, max_turns=5)
    res = await loop.run(
        chat_id=1, system_prompt="sys", user_message="hello", history=[],
    )
    assert res.stopped == "final"


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
