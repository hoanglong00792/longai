"""I3 — the only module that imports openai or hand-rolls OpenRouter HTTP.

I4 — enforces budget caps (global/per-user/per-call/wall-clock) at every call.

Patterns: stuck-loop trap, error-as-content, fallback chain, cooldown,
retryability classifier, conservative pricing fallback.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)

from longai.config import BudgetCaps
from longai.persistence import BudgetExceeded, Persistence

if TYPE_CHECKING:
    from longai.trace import Tracer

# Token prices per 1M input/output. Static table; v2 polls live pricing.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    # Paid floor (~13× cheaper than Haiku-4.5)
    "google/gemma-4-26b-a4b-it": (0.06, 0.33),
    # Conservative fallback for unknown paid models (over-bills, never under-bills)
    "_unknown_": (3.0, 15.0),
}


# Known free slugs that don't follow the `:free` suffix convention.
_FREE_EXCEPTIONS = frozenset({
    "openrouter/free",  # meta-router, free
})


def _is_free_slug(model: str) -> bool:
    """Free OpenRouter slugs: ending in ':free' OR a known free exception."""
    return model.endswith(":free") or model in _FREE_EXCEPTIONS

# Phase-2 tuning constants. Kept module-level so they're easy to find and
# adjust as we collect more model_stats data.

# B1 — soft timeout for non-tail chain entries: don't burn the full
# wall_clock budget on a model that has cheaper alternatives still queued.
# Cap = max(FLOOR, ewma_ms × MULT) for known models; for cold ones use
# wall_clock / 2. Multiplier 3× absorbs ~99th-percentile variance for
# normally-distributed latency (a 5s-EWMA model rarely spikes past 15s).
_SOFT_TIMEOUT_FLOOR_S = 8.0
_SOFT_TIMEOUT_EWMA_MULT = 3.0
_SOFT_TIMEOUT_COLD_DIVISOR = 2.0

# B2 — chain reordering trust thresholds. Below MIN_SAMPLES we treat a
# model as cold and assign DEFAULT_SCORE (≈ 30% fail rate + 10s ewma).
_COLD_START_MIN_SAMPLES = 3
_COLD_START_DEFAULT_SCORE = 40.0

# B3 — per-error-class cooldown schedule (seconds). Indexed by
# consecutive_failures - 1, clamped to last entry. Replaces the flat
# COOLDOWN_S = 300 used in Phase 1.
_COOLDOWN_SCHEDULE: dict[str, list[int]] = {
    "rate_limit": [60, 300, 600],
    "timeout":    [15, 30, 60],
    "conn_err":   [15, 30, 60],
    "server_err": [30, 60, 120],
    "other":      [60, 120, 300],
}


def _soft_timeout(
    wall_clock_s: float, *, is_last: bool, ewma_ms: float | None = None,
) -> float:
    """Tail entry keeps the full budget. Earlier entries get a model-aware
    cap: 3× the model's EWMA when known (absorbs spike variance) or
    wall_clock/2 when cold. Always bounded by [FLOOR, wall_clock_s] so a
    very slow model can't exceed the per-call budget anyway.
    """
    if is_last:
        return wall_clock_s
    if ewma_ms and ewma_ms > 0:
        cap = (ewma_ms / 1000.0) * _SOFT_TIMEOUT_EWMA_MULT
    else:
        cap = wall_clock_s / _SOFT_TIMEOUT_COLD_DIVISOR
    return min(wall_clock_s, max(_SOFT_TIMEOUT_FLOOR_S, cap))


def _cooldown_seconds(outcome: str, consecutive_failures: int) -> int:
    """Exponential backoff per error class. consecutive_failures is the
    post-failure count (1 = first consecutive failure)."""
    schedule = _COOLDOWN_SCHEDULE.get(outcome, _COOLDOWN_SCHEDULE["other"])
    idx = max(min(consecutive_failures - 1, len(schedule) - 1), 0)
    return schedule[idx]


def _score_model(stats: dict | None, original_idx: int) -> tuple[float, int]:
    """Lower = better. Cold models get a neutral score so warm-good models
    can outrank them; warm-bad models get demoted past cold.
    Tiebreak by original config order so deterministic.
    """
    if stats is None:
        return (_COLD_START_DEFAULT_SCORE, original_idx)
    total = stats["n_success"] + stats["n_error"]
    if total < _COLD_START_MIN_SAMPLES:
        return (_COLD_START_DEFAULT_SCORE, original_idx)
    fail_rate = stats["n_error"] / total
    ewma = stats["ewma_latency_ms"] or 60000.0  # all-failure fallback
    return (fail_rate * 100 + ewma / 1000.0, original_idx)


def _classify_exc(e: BaseException) -> str:
    """Map an attempt exception to a short outcome label for telemetry.
    Order matters: most specific first."""
    if isinstance(e, CallTimeout):
        return "timeout"
    if isinstance(e, APITimeoutError):
        return "timeout"
    if isinstance(e, RateLimitError):
        return "rate_limit"
    if isinstance(e, APIStatusError):
        return "server_err"
    if isinstance(e, (APIConnectionError, httpx.HTTPError)):
        return "conn_err"
    if isinstance(e, Unauthorized):
        return "unauth"
    return "other"


class Unauthorized(Exception):
    """HTTP 401 — fatal, do not fall back."""


class CallTimeout(Exception):
    """Per-call wall-clock exceeded."""


class AllModelsCooled(Exception):
    """Every model in the chain is on cooldown."""


@dataclass
class ChatResult:
    text: str
    tool_calls: list[dict] | None
    prompt_tokens: int
    completion_tokens: int
    spend_usd: float
    model_used: str
    latency_ms: float = 0.0  # wall-clock for the OpenRouter round trip


class BudgetGuard:
    """The single chokepoint to OpenRouter. I3 + I4 enforced here.

    Tier-aware: ``chat(tier=)`` selects which model chain to iterate.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        caps: BudgetCaps,
        persistence: Persistence,
        model_chains: dict[str, list[str]] | None = None,
        models: list[str] | None = None,
        prices: dict[str, tuple[float, float]] | None = None,
    ):
        # Accept either model_chains (preferred) or models (legacy single
        # chain for tests / migration). Exactly one must be supplied.
        if model_chains is None and models is None:
            raise ValueError("BudgetGuard requires model_chains= or models=")
        if model_chains is None:
            assert models is not None
            model_chains = {
                "S": list(models), "M": list(models),
                "L": list(models), "fallback": [],
            }
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._chains = {k: list(v) for k, v in model_chains.items()}
        self._caps = caps
        self._p = persistence
        self._prices = prices or DEFAULT_PRICES

    def _chain_for(self, tier: str, *, reorder_by_health: bool = False) -> list[str]:
        primary = self._chains.get(tier, [])
        fallback = self._chains.get("fallback", [])
        # Dedup while preserving order — a fallback model may also appear
        # in a tier chain on legacy configs; we don't want to retry it twice.
        seen: set[str] = set()
        primary_dedup: list[str] = []
        for m in primary:
            if m not in seen:
                seen.add(m); primary_dedup.append(m)
        fallback_dedup: list[str] = []
        for m in fallback:
            if m not in seen:
                seen.add(m); fallback_dedup.append(m)
        if reorder_by_health and primary_dedup:
            primary_dedup = self._reorder_by_health(primary_dedup)
        # Paid fallback never reordered — keeps cost discipline.
        return primary_dedup + fallback_dedup

    def _reorder_by_health(self, primary: list[str]) -> list[str]:
        """Sort primary chain by _score_model. Cold models keep config
        priority; proven-good move up; proven-bad move down."""
        stats_by_model = self._p.model_stats_for(primary)
        return [
            m for _, m in sorted(
                enumerate(primary),
                key=lambda iv: _score_model(stats_by_model.get(iv[1]), iv[0]),
            )
        ]

    @staticmethod
    def _today_utc() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _price(self, model: str) -> tuple[float, float]:
        if _is_free_slug(model):
            return (0.0, 0.0)
        return self._prices.get(model) or self._prices.get("_unknown_", (3.0, 15.0))

    def _compute_cost(self, model: str, p_tokens: int, c_tokens: int) -> float:
        in_price, out_price = self._price(model)
        return (p_tokens / 1_000_000) * in_price + (c_tokens / 1_000_000) * out_price

    async def chat(
        self,
        *,
        chat_id: int,
        messages: list[dict],
        tools: list[dict] | None,
        tier: str = "M",
        tracer: "Tracer | None" = None,
        on_chunk: Callable[[str], None] | None = None,
    ) -> ChatResult:
        # B2: reorder primary chain by health (proven-good first); fallback
        # tail kept untouched so paid models stay last.
        chain = self._chain_for(tier, reorder_by_health=True)
        cooled = self._p.cooled_models(now_ts=int(time.time()))
        # Pre-filter so we know which attempt is the tail and can grant it
        # the full wall-clock budget (B1 — soft timeout for non-tail).
        to_attempt = [m for m in chain if m not in cooled]
        if not to_attempt:
            raise AllModelsCooled(f"all of tier_{tier} chain {chain} on cooldown")
        # Single read for both ewma (B1 soft timeout) and consecutive_failures
        # (B3 cooldown) — cheap SQL, avoids re-querying per failure.
        stats_by_model = self._p.model_stats_for(to_attempt)
        for attempt_idx, model in enumerate(to_attempt):
            is_last = attempt_idx == len(to_attempt) - 1
            ewma_ms = (stats_by_model.get(model) or {}).get("ewma_latency_ms")
            try:
                return await self._attempt_call(
                    chat_id=chat_id, model=model, messages=messages, tools=tools,
                    tier=tier, tracer=tracer, attempt_idx=attempt_idx,
                    is_last=is_last, ewma_ms=ewma_ms, on_chunk=on_chunk,
                )
            except Unauthorized:
                raise  # F7: never fall back on 401
            except (RateLimitError, APIStatusError, APIConnectionError,
                    APITimeoutError, httpx.HTTPError, asyncio.TimeoutError,
                    CallTimeout) as e:
                # B3: per-error-class exponential backoff. _emit_attempt
                # already updated consecutive_failures via record_attempt.
                stats = self._p.model_stats_for([model]).get(model)
                cf = (stats or {}).get("consecutive_failures", 1)
                cooldown_s = _cooldown_seconds(_classify_exc(e), cf)
                self._p.set_cooldown(
                    model, until_ts=int(time.time()) + cooldown_s,
                )
                continue
        raise AllModelsCooled(f"all of tier_{tier} chain {chain} on cooldown")

    def _emit_attempt(
        self, tracer: "Tracer | None", model: str, tier: str,
        attempt_idx: int, outcome: str, latency_ms: float,
    ) -> None:
        """Record one chain-attempt to both the run trace and the
        per-model stats table. Called for every attempt — success or fail —
        so silent fallback no longer hides behind the winner's latency."""
        if tracer is not None:
            tracer.timing(
                "chat.attempt", latency_ms,
                model=model, tier=tier,
                attempt_idx=attempt_idx, outcome=outcome,
            )
        self._p.record_attempt(
            model=model, outcome=outcome,
            latency_ms=latency_ms, now_ts=int(time.time()),
        )

    async def _attempt_call(
        self, *, chat_id: int, model: str, messages: list[dict],
        tools: list[dict] | None, tier: str = "M",
        tracer: "Tracer | None" = None, attempt_idx: int = 0,
        is_last: bool = True, ewma_ms: float | None = None,
        on_chunk: Callable[[str], None] | None = None,
    ) -> ChatResult:
        wall_clock = self._caps.wall_clock_for(tier)
        # B1: only the tail attempt keeps the full wall-clock; earlier
        # entries get a model-aware cap (3× their EWMA when known).
        timeout_s = _soft_timeout(wall_clock, is_last=is_last, ewma_ms=ewma_ms)
        t0 = time.perf_counter()
        try:
            async with asyncio.timeout(timeout_s):
                if on_chunk is not None:
                    text, tool_calls, p_tokens, c_tokens = (
                        await self._raw_call_streaming(
                            model=model, messages=messages, tools=tools,
                            on_chunk=on_chunk,
                        )
                    )
                else:
                    resp = await self._raw_call(
                        model=model, messages=messages, tools=tools,
                    )
                    choice = resp.choices[0].message
                    text = getattr(choice, "content", "") or ""
                    tool_calls = [
                        {"id": tc.id, "name": tc.function.name,
                         "arguments": tc.function.arguments}
                        for tc in (getattr(choice, "tool_calls", None) or [])
                    ] or None
                    p_tokens = resp.usage.prompt_tokens
                    c_tokens = resp.usage.completion_tokens
        except asyncio.TimeoutError as e:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            self._emit_attempt(tracer, model, tier, attempt_idx, "timeout", latency_ms)
            raise CallTimeout(f"call to {model} exceeded {timeout_s:.0f}s") from e
        except Exception as e:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            self._emit_attempt(
                tracer, model, tier, attempt_idx,
                _classify_exc(e), latency_ms,
            )
            raise
        latency_ms = (time.perf_counter() - t0) * 1000.0
        self._emit_attempt(tracer, model, tier, attempt_idx, "success", latency_ms)

        cost = self._compute_cost(model, p_tokens, c_tokens)

        # Per-call cap (the per_call_max_usd is checked here).
        if cost > self._caps.per_call_max_usd:
            raise BudgetExceeded("per_call", cost, self._caps.per_call_max_usd)

        # Atomic debit with global+per-user cap check (F1, F2, F3).
        self._p.debit(
            chat_id=chat_id, model=model, usd=cost, day_utc=self._today_utc(),
            per_user_cap=self._caps.per_user_daily_usd,
            global_cap=self._caps.global_daily_usd,
        )

        return ChatResult(
            text=text,
            tool_calls=tool_calls,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            spend_usd=cost,
            model_used=model,
            latency_ms=latency_ms,
        )

    async def _raw_call_streaming(
        self, *, model: str, messages: list[dict], tools: list[dict] | None,
        on_chunk: Callable[[str], None],
    ) -> tuple[str, list[dict] | None, int, int]:
        """Streaming variant of _raw_call. Iterates SSE chunks, calling
        ``on_chunk(text_delta)`` for each text fragment. Tool-call deltas
        are accumulated by ``index`` per the OpenAI streaming spec.

        Returns (text, tool_calls, prompt_tokens, completion_tokens).
        Token counts come from the final usage chunk (stream_options
        include_usage=True). Falls back to 0/0 when the provider doesn't
        emit usage on stream — caller treats 0-cost as $0 spend, fine
        for free models.
        """
        try:
            stream = await self._client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto" if tools else None,
                stream=True,
                stream_options={"include_usage": True},
            )
        except AuthenticationError as e:
            raise Unauthorized(str(e)) from e
        text_parts: list[str] = []
        tc_acc: dict[int, dict[str, str]] = {}
        p_tokens = 0
        c_tokens = 0
        async for chunk in stream:
            if getattr(chunk, "usage", None):
                p_tokens = chunk.usage.prompt_tokens
                c_tokens = chunk.usage.completion_tokens
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content_delta = getattr(delta, "content", None)
            if content_delta:
                text_parts.append(content_delta)
                on_chunk(content_delta)
            for tc_delta in getattr(delta, "tool_calls", None) or []:
                idx = tc_delta.index
                entry = tc_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if getattr(tc_delta, "id", None):
                    entry["id"] = tc_delta.id
                fn = getattr(tc_delta, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        entry["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        entry["arguments"] += fn.arguments
        text = "".join(text_parts)
        tool_calls = (
            [tc_acc[i] for i in sorted(tc_acc.keys())] if tc_acc else None
        )
        return text, tool_calls, p_tokens, c_tokens

    async def _raw_call(
        self, *, model: str, messages: list[dict], tools: list[dict] | None,
    ):
        """Single OpenRouter chat-completion call. Wraps openai SDK auth errors.

        This is the ONLY method in the codebase that talks to the openai client.
        """
        try:
            return await self._client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto" if tools else None,
            )
        except AuthenticationError as e:
            raise Unauthorized(str(e)) from e
