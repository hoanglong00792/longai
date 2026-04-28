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
    # Latency populated by _attempt_call timing wrap
    assert res.latency_ms >= 0.0


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


# ── Tier-aware routing ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tier_l_uses_tier_l_chain(caps_tiny, persistence, monkeypatch):
    """tier='L' iterates the L chain (then fallback), not S or M."""
    g = BudgetGuard(
        api_key="sk", base_url="x",
        model_chains={
            "S": ["small"], "M": ["mid"], "L": ["big-1", "big-2"],
            "fallback": ["paid"],
        },
        caps=caps_tiny, persistence=persistence,
        prices={s: (0, 0) for s in ("small", "mid", "big-1", "big-2", "paid")},
    )
    monkeypatch.setattr(g, "_raw_call", AsyncMock(return_value=_mk_response("ok", model="big-1")))
    res = await g.chat(
        chat_id=1, messages=[{"role": "user", "content": "x"}],
        tools=None, tier="L",
    )
    assert res.model_used == "big-1"


@pytest.mark.asyncio
async def test_tier_falls_through_to_fallback(caps_tiny, persistence, monkeypatch):
    """When the tier's chain is fully cooled, fallback chain is tried."""
    persistence.set_cooldown("big-1", until_ts=10**12)
    g = BudgetGuard(
        api_key="sk", base_url="x",
        model_chains={"S": [], "M": ["mid"], "L": ["big-1"], "fallback": ["paid"]},
        caps=caps_tiny, persistence=persistence,
        prices={"mid": (0, 0), "big-1": (0, 0), "paid": (1, 1)},
    )
    monkeypatch.setattr(g, "_raw_call", AsyncMock(return_value=_mk_response("ok", model="paid")))
    res = await g.chat(
        chat_id=1, messages=[{"role": "user", "content": "x"}],
        tools=None, tier="L",
    )
    assert res.model_used == "paid"


@pytest.mark.asyncio
async def test_legacy_models_kwarg_routes_all_tiers_same(caps_tiny, persistence, monkeypatch):
    """Backward compat: legacy `models=[...]` populates every tier identically."""
    g = BudgetGuard(
        api_key="sk", base_url="x", models=["m1"],
        caps=caps_tiny, persistence=persistence, prices={"m1": (1, 1)},
    )
    monkeypatch.setattr(g, "_raw_call", AsyncMock(return_value=_mk_response("ok", model="m1")))
    for tier in ("S", "M", "L"):
        res = await g.chat(
            chat_id=1, messages=[{"role": "user", "content": "x"}],
            tools=None, tier=tier,
        )
        assert res.model_used == "m1"


@pytest.mark.asyncio
async def test_wall_clock_uses_tier_override(caps_tiny, persistence, monkeypatch):
    """tier_L wall-clock override extends the per-call timeout."""
    caps_tiny.by_tier = {"L": {"per_call_wall_clock_s": 60}}
    g = BudgetGuard(
        api_key="sk", base_url="x", models=["m1"],
        caps=caps_tiny, persistence=persistence, prices={"m1": (1, 1)},
    )
    captured: list[int] = []

    async def fake_call(model, messages, tools):
        # Probe what timeout context was active by reading caps
        return _mk_response("ok", model=model)

    monkeypatch.setattr(g, "_raw_call", AsyncMock(side_effect=fake_call))
    # Just verify the path works at tier=L; explicit wall-clock probe is
    # done indirectly via _attempt_call's asyncio.timeout, which we can't
    # introspect post-hoc without monkeypatching asyncio.
    res = await g.chat(
        chat_id=1, messages=[{"role": "user", "content": "x"}],
        tools=None, tier="L",
    )
    assert res.model_used == "m1"
    # Ensure the caps method returns the override
    assert caps_tiny.wall_clock_for("L") == 60
    assert caps_tiny.wall_clock_for("M") == 5  # default from caps_tiny


@pytest.mark.asyncio
async def test_chat_attempt_traces_every_chain_step(
    caps_tiny, persistence, monkeypatch, tmp_path,
):
    """Silent fallback regression guard: when chain falls A→B, both attempts
    must be emitted as chat.attempt records and persisted to model_stats."""
    from longai.trace import Tracer
    from openai import RateLimitError

    g = BudgetGuard(
        api_key="sk", base_url="x", models=["m1", "m2"],
        caps=caps_tiny, persistence=persistence,
        prices={"m1": (0, 0), "m2": (0, 0)},
    )
    # m1 fails with RateLimitError, m2 succeeds.
    rate_limit = RateLimitError(
        message="429", response=MagicMock(status_code=429),
        body={"error": "rate limited"},
    )
    fake = AsyncMock(side_effect=[rate_limit, _mk_response("ok", model="m2")])
    monkeypatch.setattr(g, "_raw_call", fake)

    tracer = Tracer(str(tmp_path))
    res = await g.chat(
        chat_id=1, messages=[{"role": "user", "content": "x"}],
        tools=None, tracer=tracer,
    )
    assert res.model_used == "m2"

    # Trace file: exactly 2 chat.attempt rows in order m1(fail), m2(success).
    import json
    [timings_file] = list(tmp_path.rglob("06_timings.jsonl"))
    rows = [
        json.loads(line) for line in timings_file.read_text().splitlines()
    ]
    attempts = [r for r in rows if r["phase"] == "chat.attempt"]
    assert len(attempts) == 2
    assert attempts[0]["model"] == "m1"
    assert attempts[0]["outcome"] == "rate_limit"
    assert attempts[0]["attempt_idx"] == 0
    assert attempts[1]["model"] == "m2"
    assert attempts[1]["outcome"] == "success"
    assert attempts[1]["attempt_idx"] == 1

    # model_stats persisted.
    stats = {
        row["model"]: dict(row) for row in persistence._conn.execute(
            "SELECT * FROM model_stats"
        )
    }
    assert stats["m1"]["n_error"] == 1 and stats["m1"]["n_success"] == 0
    assert stats["m2"]["n_success"] == 1 and stats["m2"]["n_error"] == 0
    assert stats["m2"]["ewma_latency_ms"] is not None


# ── Phase 2: B1 + B2 + B3 ──────────────────────────────────────────────


def test_soft_timeout_full_for_last_capped_for_others():
    from longai.budget_guard import _soft_timeout
    # Tail entry keeps full budget regardless of EWMA
    assert _soft_timeout(30.0, is_last=True) == 30.0
    assert _soft_timeout(30.0, is_last=True, ewma_ms=1000.0) == 30.0
    # Cold model (no EWMA): wall/2, floored at 8s, clamped to wall_clock
    assert _soft_timeout(30.0, is_last=False) == 15.0
    assert _soft_timeout(15.0, is_last=False) == 8.0
    # When wall_clock < floor, wall_clock wins (can't exceed per-call budget)
    assert _soft_timeout(5.0, is_last=False) == 5.0
    # Known fast model (5s EWMA): 3× = 15s soft timeout
    assert _soft_timeout(30.0, is_last=False, ewma_ms=5000.0) == 15.0
    # Known slow-ish model (10s EWMA): 3× = 30s, clamped to wall_clock
    assert _soft_timeout(30.0, is_last=False, ewma_ms=10000.0) == 30.0
    # Very fast model still gets the floor
    assert _soft_timeout(30.0, is_last=False, ewma_ms=500.0) == 8.0
    # ewma_ms=0 / None / negative all treated as cold
    assert _soft_timeout(30.0, is_last=False, ewma_ms=0) == 15.0
    assert _soft_timeout(30.0, is_last=False, ewma_ms=None) == 15.0


def test_cooldown_seconds_per_error_class_with_backoff():
    from longai.budget_guard import _cooldown_seconds
    # rate_limit: 60 → 300 → 600 (clamped at last)
    assert _cooldown_seconds("rate_limit", 1) == 60
    assert _cooldown_seconds("rate_limit", 2) == 300
    assert _cooldown_seconds("rate_limit", 3) == 600
    assert _cooldown_seconds("rate_limit", 99) == 600
    # timeout & conn_err: 15 → 30 → 60 — much shorter
    assert _cooldown_seconds("timeout", 1) == 15
    assert _cooldown_seconds("conn_err", 1) == 15
    # server_err: middle severity
    assert _cooldown_seconds("server_err", 1) == 30
    # unknown class → "other" schedule
    assert _cooldown_seconds("weirdo", 1) == 60
    # consecutive_failures=0 (shouldn't happen post-failure but be safe)
    assert _cooldown_seconds("rate_limit", 0) == 60


def test_score_model_cold_start_keeps_config_order():
    from longai.budget_guard import _score_model, _COLD_START_DEFAULT_SCORE
    # Cold model: score = default, tiebreak by original_idx
    assert _score_model(None, 0) == (_COLD_START_DEFAULT_SCORE, 0)
    assert _score_model(None, 5) == (_COLD_START_DEFAULT_SCORE, 5)
    # Below MIN_SAMPLES → still cold
    stats_thin = {"n_success": 1, "n_error": 1, "ewma_latency_ms": 100.0}
    assert _score_model(stats_thin, 2) == (_COLD_START_DEFAULT_SCORE, 2)


def test_score_model_promotes_proven_good_demotes_proven_bad():
    from longai.budget_guard import _score_model, _COLD_START_DEFAULT_SCORE
    proven_good = {"n_success": 10, "n_error": 0, "ewma_latency_ms": 5000.0}
    proven_bad = {"n_success": 1, "n_error": 9, "ewma_latency_ms": 25000.0}
    score_good = _score_model(proven_good, 5)[0]
    score_bad = _score_model(proven_bad, 0)[0]
    score_cold = _score_model(None, 2)[0]
    # proven_good: 0 * 100 + 5000/1000 = 5
    assert score_good == pytest.approx(5.0)
    # proven_bad: 0.9 * 100 + 25000/1000 = 115
    assert score_bad == pytest.approx(115.0)
    # ordering: good < cold < bad
    assert score_good < score_cold < score_bad


@pytest.mark.asyncio
async def test_chain_reordering_promotes_proven_good_model(
    caps_tiny, persistence, monkeypatch,
):
    """Config order: bad, good. Stats show 'bad' fails 100% and 'good' is
    fast+reliable. Reorder should put 'good' first."""
    # Seed stats: 'bad' = 5 rate_limits, 'good' = 5 successes @ 2000ms
    for _ in range(5):
        persistence.record_attempt(model="bad", outcome="rate_limit",
                                   latency_ms=1.0, now_ts=1)
        persistence.record_attempt(model="good", outcome="success",
                                   latency_ms=2000.0, now_ts=2)
    g = BudgetGuard(
        api_key="sk", base_url="x", models=["bad", "good"],
        caps=caps_tiny, persistence=persistence,
        prices={"bad": (0, 0), "good": (0, 0)},
    )
    monkeypatch.setattr(
        g, "_raw_call", AsyncMock(return_value=_mk_response("ok", model="good")),
    )
    res = await g.chat(chat_id=1, messages=[{"role": "user", "content": "x"}],
                       tools=None)
    assert res.model_used == "good"
    # 'bad' should never have been called — only 1 attempt for 'good'
    assert g._raw_call.call_count == 1


@pytest.mark.asyncio
async def test_paid_fallback_never_promoted_above_free(
    caps_tiny, persistence, monkeypatch,
):
    """Even if paid/floor has a great record and free 'm1' is bad,
    the paid floor stays last (cost discipline)."""
    persistence.record_attempt(model="paid", outcome="success",
                               latency_ms=500.0, now_ts=1)
    persistence.record_attempt(model="paid", outcome="success",
                               latency_ms=500.0, now_ts=2)
    persistence.record_attempt(model="paid", outcome="success",
                               latency_ms=500.0, now_ts=3)
    # m1 has 3 rate limits — score 100+
    for _ in range(3):
        persistence.record_attempt(model="m1", outcome="rate_limit",
                                   latency_ms=1.0, now_ts=4)
    g = BudgetGuard(
        api_key="sk", base_url="x",
        model_chains={"S": ["m1"], "M": ["m1"], "L": ["m1"], "fallback": ["paid"]},
        caps=caps_tiny, persistence=persistence,
        prices={"m1": (0, 0), "paid": (0, 0)},
    )
    chain = g._chain_for("M", reorder_by_health=True)
    # Even though m1 is proven-bad, it stays in primary; paid stays at tail.
    assert chain == ["m1", "paid"]


@pytest.mark.asyncio
async def test_b3_cooldown_uses_per_error_class_duration(
    caps_tiny, persistence, monkeypatch,
):
    """First rate_limit failure should cool the model for 60s
    (not the old flat 300s), and conn_err for 15s."""
    from openai import APIConnectionError, RateLimitError
    g = BudgetGuard(
        api_key="sk", base_url="x", models=["m1", "m2"],
        caps=caps_tiny, persistence=persistence,
        prices={"m1": (0, 0), "m2": (0, 0)},
    )
    # m1 rate-limits, m2 succeeds.
    rate_limit = RateLimitError(
        message="429", response=MagicMock(status_code=429),
        body={"error": "x"},
    )
    monkeypatch.setattr(
        g, "_raw_call",
        AsyncMock(side_effect=[rate_limit, _mk_response("ok", model="m2")]),
    )
    captured: dict = {}
    real_set = persistence.set_cooldown
    def spy(model, *, until_ts):
        captured.setdefault(model, until_ts)
        return real_set(model, until_ts=until_ts)
    monkeypatch.setattr(persistence, "set_cooldown", spy)
    t_before = int(__import__("time").time())
    await g.chat(chat_id=1, messages=[{"role": "user", "content": "x"}], tools=None)
    # m1 cooled ~60s into the future (rate_limit, 1st consecutive failure)
    cooldown_s = captured["m1"] - t_before
    assert 55 <= cooldown_s <= 65


# ── Phase C4: streaming ─────────────────────────────────────────────────


def _mk_stream_chunk(*, content: str | None = None,
                      tool_calls: list[dict] | None = None,
                      usage: tuple[int, int] | None = None):
    """Build an async iterator of fake OpenAI streaming-shape chunks."""
    chunk = MagicMock()
    if usage is not None:
        chunk.usage = MagicMock(prompt_tokens=usage[0], completion_tokens=usage[1])
    else:
        chunk.usage = None
    if content is None and not tool_calls:
        chunk.choices = []
    else:
        delta = MagicMock()
        delta.content = content
        if tool_calls:
            built = []
            for tc in tool_calls:
                tcd = MagicMock()
                tcd.index = tc["index"]
                tcd.id = tc.get("id")
                if "name" in tc or "arguments" in tc:
                    fn = MagicMock()
                    fn.name = tc.get("name")
                    fn.arguments = tc.get("arguments")
                    tcd.function = fn
                else:
                    tcd.function = None
                built.append(tcd)
            delta.tool_calls = built
        else:
            delta.tool_calls = None
        choice = MagicMock(); choice.delta = delta
        chunk.choices = [choice]
    return chunk


@pytest.mark.asyncio
async def test_streaming_invokes_on_chunk_per_text_delta(
    caps_tiny, persistence, monkeypatch,
):
    """on_chunk callback fires for each text delta; final ChatResult has
    accumulated text and proper token counts."""
    g = BudgetGuard(
        api_key="sk", base_url="x", models=["m1"],
        caps=caps_tiny, persistence=persistence, prices={"m1": (0, 0)},
    )

    async def fake_stream():
        yield _mk_stream_chunk(content="Hello ")
        yield _mk_stream_chunk(content="world!")
        yield _mk_stream_chunk(usage=(100, 20))

    async def fake_create(**kwargs):
        assert kwargs["stream"] is True
        return fake_stream()

    monkeypatch.setattr(g._client.chat.completions, "create", fake_create)
    chunks: list[str] = []
    res = await g.chat(
        chat_id=1, messages=[{"role": "user", "content": "x"}],
        tools=None, on_chunk=chunks.append,
    )
    assert chunks == ["Hello ", "world!"]
    assert res.text == "Hello world!"
    assert res.tool_calls is None
    assert res.prompt_tokens == 100
    assert res.completion_tokens == 20


@pytest.mark.asyncio
async def test_streaming_accumulates_tool_call_arguments_across_chunks(
    caps_tiny, persistence, monkeypatch,
):
    """Tool-call args stream as fragments; we must concatenate by index."""
    g = BudgetGuard(
        api_key="sk", base_url="x", models=["m1"],
        caps=caps_tiny, persistence=persistence, prices={"m1": (0, 0)},
    )

    async def fake_stream():
        yield _mk_stream_chunk(tool_calls=[
            {"index": 0, "id": "call_1", "name": "calculate", "arguments": '{"exp'},
        ])
        yield _mk_stream_chunk(tool_calls=[
            {"index": 0, "arguments": 'r":"2+2"}'},
        ])
        yield _mk_stream_chunk(usage=(50, 10))

    async def fake_create(**kwargs):
        return fake_stream()

    monkeypatch.setattr(g._client.chat.completions, "create", fake_create)
    res = await g.chat(
        chat_id=1, messages=[{"role": "user", "content": "x"}],
        tools=[{"type": "function", "function": {"name": "calculate"}}],
        on_chunk=lambda c: None,
    )
    assert res.tool_calls == [{
        "id": "call_1", "name": "calculate", "arguments": '{"expr":"2+2"}',
    }]


def test_budget_guard_requires_chains_or_models(caps_tiny, persistence):
    with pytest.raises(ValueError, match="model_chains"):
        BudgetGuard(
            api_key="sk", base_url="x",
            caps=caps_tiny, persistence=persistence,
        )
