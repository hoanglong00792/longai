"""I9 — JSON envelope.

The OpenRouter wire returns usage.prompt_tokens / completion_tokens.
The Anthropic-compatible rename to input_tokens / output_tokens happens HERE,
on every path including errors. Doyen has flagged this rename three times
across docs.
"""
from __future__ import annotations

from typing import Any, Protocol


class _ResultLike(Protocol):
    text: str
    prompt_tokens: int
    completion_tokens: int
    spend_usd: float
    turns: int
    stopped: str
    tier: str
    error: str | None


def format_result(r: _ResultLike, *, model: str, trace_id: str) -> dict[str, Any]:
    return {
        "result": r.text,
        "usage": {
            "input_tokens": r.prompt_tokens,
            "output_tokens": r.completion_tokens,
        },
        "model": model,
        "tier": getattr(r, "tier", "M"),
        "turns": r.turns,
        "stopped": r.stopped,
        "spend_usd": r.spend_usd,
        "trace_id": trace_id,
        "error": r.error,
    }


def format_error(exc: BaseException, *, trace_id: str) -> dict[str, Any]:
    return {
        "result": f"Sorry — {exc}. Try again.",
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "model": "",
        "tier": "M",
        "turns": 0,
        "stopped": "error",
        "spend_usd": 0.0,
        "trace_id": trace_id,
        "error": str(exc),
    }
