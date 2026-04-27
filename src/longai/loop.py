"""Agent loop — single-turn or multi-turn until final|max_turns|stuck|budget|error.

Patterns lifted from dr-agent/internal/agent/loop.go:
- error-as-content: tool errors become {"error":"..."} content fed back
- stuck-loop trap: 3 identical (name, args) SHA-256 hashes → bail
- stateless per-call: every run rebuilds messages, hashes, tokens
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from longai.budget_guard import (
    AllModelsCooled,
    BudgetGuard,
    CallTimeout,
    Unauthorized,
)
from longai.mcp_client import MCPRegistry
from longai.persistence import BudgetExceeded
from longai.security import sanitize_tool_output


@dataclass
class LoopResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    spend_usd: float
    turns: int
    stopped: str  # "final" | "max_turns" | "stuck_loop" | "budget" | "timeout" | "error"
    model_used: str
    error: str | None = None


class Loop:
    def __init__(self, *, guard: BudgetGuard, mcp: MCPRegistry, max_turns: int = 5):
        self._g = guard
        self._mcp = mcp
        self._max_turns = max_turns

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
    ) -> LoopResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *[{"role": m["role"], "content": m["content"]} for m in history],
            {"role": "user", "content": user_message},
        ]

        recent_hashes: list[str] = []
        total_p = total_c = 0
        total_spend = 0.0
        last_model = ""

        for turn in range(1, self._max_turns + 1):
            try:
                cr = await self._g.chat(
                    chat_id=chat_id, messages=messages, tools=self._mcp.tools() or None,
                )
            except BudgetExceeded as e:
                return LoopResult(
                    text=f"Daily budget reached ({e.scope}). Resets at UTC midnight.",
                    prompt_tokens=total_p, completion_tokens=total_c,
                    spend_usd=total_spend, turns=turn - 1, stopped="budget",
                    model_used=last_model, error=str(e),
                )
            except CallTimeout as e:
                return LoopResult(
                    text="That took too long. Try again with a simpler request.",
                    prompt_tokens=total_p, completion_tokens=total_c,
                    spend_usd=total_spend, turns=turn - 1, stopped="timeout",
                    model_used=last_model, error=str(e),
                )
            except (Unauthorized, AllModelsCooled) as e:
                return LoopResult(
                    text="LLM provider unavailable. Try again in a few minutes.",
                    prompt_tokens=total_p, completion_tokens=total_c,
                    spend_usd=total_spend, turns=turn - 1, stopped="error",
                    model_used=last_model, error=str(e),
                )

            total_p += cr.prompt_tokens
            total_c += cr.completion_tokens
            total_spend += cr.spend_usd
            last_model = cr.model_used

            # No tool calls → final answer
            if not cr.tool_calls:
                return LoopResult(
                    text=cr.text, prompt_tokens=total_p, completion_tokens=total_c,
                    spend_usd=total_spend, turns=turn, stopped="final",
                    model_used=last_model,
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
                    and turn < self._max_turns
                ):
                    return LoopResult(
                        text=f"Got stuck calling `{tc['name']}` repeatedly. Try rephrasing.",
                        prompt_tokens=total_p, completion_tokens=total_c,
                        spend_usd=total_spend, turns=turn, stopped="stuck_loop",
                        model_used=last_model,
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
                    raw = await self._mcp.call(tc["name"], args_obj)
                    sanitized = sanitize_tool_output(raw)
                messages.append({
                    "role": "tool", "tool_call_id": tc["id"], "content": sanitized,
                })

        return LoopResult(
            text="(stopped at max turns; not enough context to finalize)",
            prompt_tokens=total_p, completion_tokens=total_c,
            spend_usd=total_spend, turns=self._max_turns, stopped="max_turns",
            model_used=last_model,
        )
