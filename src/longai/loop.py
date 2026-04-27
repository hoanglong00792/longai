"""Agent loop — single-turn or multi-turn until final|max_turns|stuck|budget|error.

Patterns:
- error-as-content: tool errors become {"error":"..."} content fed back
- stuck-loop trap: 3 identical (name, args) SHA-256 hashes → bail
- stateless per-call: every run rebuilds messages, hashes, tokens
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from longai.trace import Tracer

from longai.budget_guard import (
    AllModelsCooled,
    BudgetGuard,
    CallTimeout,
    Unauthorized,
)
from longai.config import BudgetCaps
from longai.mcp_client import MCPRegistry
from longai.persistence import BudgetExceeded
from longai.security import sanitize_tool_output
from longai.tier_classifier import classify as classify_tier


@dataclass
class LoopResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    spend_usd: float
    turns: int
    stopped: str  # "final" | "max_turns" | "stuck_loop" | "budget" | "timeout" | "error"
    model_used: str
    tier: str = "M"
    error: str | None = None


class Loop:
    def __init__(
        self,
        *,
        guard: BudgetGuard,
        mcp: MCPRegistry,
        max_turns: int | None = None,
        caps: BudgetCaps | None = None,
    ):
        """Wire either a flat ``max_turns`` (tests, single-tier mode) or a
        ``caps`` object that supplies per-tier limits via ``turns_for(tier)``.
        If both are given, ``max_turns`` overrides — useful for forcing a
        bounded loop in tests regardless of tier-routed defaults.
        """
        self._g = guard
        self._mcp = mcp
        self._max_turns_override = max_turns
        self._caps = caps

    def _max_turns_for(self, tier: str) -> int:
        if self._max_turns_override is not None:
            return self._max_turns_override
        if self._caps is not None:
            return self._caps.turns_for(tier)
        return 5

    # Tier ordering: S < M < L. Used by mid-loop complexity bumps so a
    # heavy skill loaded on turn 2 escalates the rest of the run.
    _TIER_ORDER = {"S": 0, "M": 1, "L": 2}

    def _maybe_bump_tier(
        self, tool_name: str, tool_result_json: str,
        tier: str, max_turns: int,
    ) -> tuple[str, int]:
        """If *tool_name* is ``load_skill`` and the result declares a higher
        complexity than *tier*, bump tier and extend *max_turns*. Never
        downgrades. Silent on parse failures or invalid complexity values.
        """
        if tool_name != "load_skill":
            return tier, max_turns
        try:
            data = json.loads(tool_result_json)
        except (json.JSONDecodeError, TypeError):
            return tier, max_turns
        if not isinstance(data, dict):
            return tier, max_turns
        new_complexity = data.get("complexity")
        if new_complexity not in self._TIER_ORDER:
            return tier, max_turns
        if self._TIER_ORDER[new_complexity] <= self._TIER_ORDER[tier]:
            return tier, max_turns
        new_max = max(max_turns, self._max_turns_for(new_complexity))
        return new_complexity, new_max

    @staticmethod
    def _hash_call(name: str, args_str: str) -> str:
        try:
            args_obj = json.loads(args_str) if args_str else {}
            normalized = json.dumps(args_obj, sort_keys=True)
        except Exception:
            normalized = args_str
        return hashlib.sha256(f"{name}|{normalized}".encode()).hexdigest()

    async def run(
        self,
        *,
        chat_id: int,
        system_prompt: str,
        user_message: str,
        history: list[dict[str, Any]],
        tier: str | None = None,
        tracer: "Tracer | None" = None,
    ) -> LoopResult:
        # Classify if no explicit tier given. The classifier may also strip
        # /deep // /quick prefix noise from the message before forwarding.
        if tier is None:
            tier, user_message = classify_tier(user_message)

        max_turns = self._max_turns_for(tier)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *[{"role": m["role"], "content": m["content"]} for m in history],
            {"role": "user", "content": user_message},
        ]

        recent_hashes: list[str] = []
        total_p = total_c = 0
        total_spend = 0.0
        last_model = ""

        for turn in range(1, max_turns + 1):
            turn_t0 = time.perf_counter()
            try:
                cr = await self._g.chat(
                    chat_id=chat_id, messages=messages,
                    tools=self._mcp.tools() or None, tier=tier,
                )
            except BudgetExceeded as e:
                return LoopResult(
                    text=f"Daily budget reached ({e.scope}). Resets at UTC midnight.",
                    prompt_tokens=total_p, completion_tokens=total_c,
                    spend_usd=total_spend, turns=turn - 1, stopped="budget",
                    model_used=last_model, tier=tier, error=str(e),
                )
            except CallTimeout as e:
                return LoopResult(
                    text="That took too long. Try again with a simpler request.",
                    prompt_tokens=total_p, completion_tokens=total_c,
                    spend_usd=total_spend, turns=turn - 1, stopped="timeout",
                    model_used=last_model, tier=tier, error=str(e),
                )
            except (Unauthorized, AllModelsCooled) as e:
                return LoopResult(
                    text="LLM provider unavailable. Try again in a few minutes.",
                    prompt_tokens=total_p, completion_tokens=total_c,
                    spend_usd=total_spend, turns=turn - 1, stopped="error",
                    model_used=last_model, tier=tier, error=str(e),
                )

            total_p += cr.prompt_tokens
            total_c += cr.completion_tokens
            total_spend += cr.spend_usd
            last_model = cr.model_used

            if tracer is not None:
                tracer.timing("chat", cr.latency_ms, model=cr.model_used,
                              tier=tier, turn=turn,
                              prompt_tokens=cr.prompt_tokens,
                              completion_tokens=cr.completion_tokens)

            # No tool calls → final answer
            if not cr.tool_calls:
                if tracer is not None:
                    turn_ms = (time.perf_counter() - turn_t0) * 1000.0
                    tracer.timing("turn", turn_ms, turn=turn, stopped="final")
                return LoopResult(
                    text=cr.text, prompt_tokens=total_p, completion_tokens=total_c,
                    spend_usd=total_spend, turns=turn, stopped="final",
                    model_used=last_model, tier=tier,
                )

            # Append assistant message with tool_calls (in OpenAI shape)
            messages.append({
                "role": "assistant",
                "content": cr.text or "",
                "tool_calls": [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for tc in cr.tool_calls
                ],
            })

            # Stuck-loop trap (F6): hash each tool call; if same hash 3× in a row, bail.
            # Only bail if we have remaining turns; if this is the last turn, max_turns wins.
            for tc in cr.tool_calls:
                h = self._hash_call(tc["name"], tc.get("arguments", ""))
                recent_hashes.append(h)
                recent_hashes = recent_hashes[-3:]
                if (
                    len(recent_hashes) == 3
                    and len(set(recent_hashes)) == 1
                    and turn < max_turns
                ):
                    return LoopResult(
                        text=f"Got stuck calling `{tc['name']}` repeatedly. Try rephrasing.",
                        prompt_tokens=total_p, completion_tokens=total_c,
                        spend_usd=total_spend, turns=turn, stopped="stuck_loop",
                        model_used=last_model, tier=tier,
                        error=f"stuck on {tc['name']}",
                    )

            # Dispatch each tool call; sanitize results before appending
            for tc in cr.tool_calls:
                try:
                    args_obj = json.loads(tc.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args_obj = {}
                    sanitized = json.dumps({"error": "malformed args"})
                else:
                    tool_t0 = time.perf_counter()
                    raw = await self._mcp.call(tc["name"], args_obj)
                    sanitized = sanitize_tool_output(raw)
                    if tracer is not None:
                        tool_ms = (time.perf_counter() - tool_t0) * 1000.0
                        tracer.timing("tool", tool_ms, name=tc["name"], turn=turn)
                messages.append({
                    "role": "tool", "tool_call_id": tc["id"], "content": sanitized,
                })
                # Mid-loop tier escalation: when a heavy skill is loaded,
                # upgrade tier (and extend turn budget) for the remaining
                # iterations. Sanitize-then-bump order is intentional: we
                # parse the same payload the LLM will see.
                tier, max_turns = self._maybe_bump_tier(
                    tc["name"], sanitized, tier, max_turns,
                )

            if tracer is not None:
                turn_ms = (time.perf_counter() - turn_t0) * 1000.0
                tracer.timing("turn", turn_ms, turn=turn,
                              tool_calls=len(cr.tool_calls))

        return LoopResult(
            text="(stopped at max turns; not enough context to finalize)",
            prompt_tokens=total_p, completion_tokens=total_c,
            spend_usd=total_spend, turns=max_turns, stopped="max_turns",
            model_used=last_model, tier=tier,
        )
