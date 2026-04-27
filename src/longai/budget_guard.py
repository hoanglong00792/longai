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

COOLDOWN_S = 300  # 5 minutes — bench-press a model after a retryable failure


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

    def _chain_for(self, tier: str) -> list[str]:
        primary = self._chains.get(tier, [])
        fallback = self._chains.get("fallback", [])
        # Dedup while preserving order — a fallback model may also appear
        # in a tier chain on legacy configs; we don't want to retry it twice.
        seen: set[str] = set()
        out: list[str] = []
        for m in primary + fallback:
            if m not in seen:
                seen.add(m); out.append(m)
        return out

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
    ) -> ChatResult:
        chain = self._chain_for(tier)
        cooled = self._p.cooled_models(now_ts=int(time.time()))
        for model in chain:
            if model in cooled:
                continue
            try:
                return await self._attempt_call(
                    chat_id=chat_id, model=model, messages=messages, tools=tools,
                    tier=tier,
                )
            except Unauthorized:
                raise  # F7: never fall back on 401
            except (RateLimitError, APIStatusError, APIConnectionError,
                    APITimeoutError, httpx.HTTPError, asyncio.TimeoutError,
                    CallTimeout) as e:
                # F8-F11 + slow-free-model timeout: retryable.
                # CallTimeout (per-call wall-clock exceeded) benches the slow model
                # and tries the next — a free 120B model timing out should not kill
                # the whole call when faster models exist in the chain.
                self._p.set_cooldown(model, until_ts=int(time.time()) + COOLDOWN_S)
                continue
        raise AllModelsCooled(f"all of tier_{tier} chain {chain} on cooldown")

    async def _attempt_call(
        self, *, chat_id: int, model: str, messages: list[dict],
        tools: list[dict] | None, tier: str = "M",
    ) -> ChatResult:
        wall_clock = self._caps.wall_clock_for(tier)
        t0 = time.perf_counter()
        try:
            async with asyncio.timeout(wall_clock):
                resp = await self._raw_call(model=model, messages=messages, tools=tools)
        except asyncio.TimeoutError as e:
            raise CallTimeout(f"call to {model} exceeded {wall_clock}s") from e
        latency_ms = (time.perf_counter() - t0) * 1000.0

        # Compute cost from usage
        usage = resp.usage
        p_tokens = usage.prompt_tokens
        c_tokens = usage.completion_tokens
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

        choice = resp.choices[0].message
        return ChatResult(
            text=getattr(choice, "content", "") or "",
            tool_calls=[
                {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                for tc in (getattr(choice, "tool_calls", None) or [])
            ] or None,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            spend_usd=cost,
            model_used=model,
            latency_ms=latency_ms,
        )

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
