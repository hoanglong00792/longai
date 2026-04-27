"""Map a user message to a model tier (S | M | L).

Tiers feed BudgetGuard.chat(tier=...) which selects the model chain. The
classifier is intentionally cheap — regex over the message — because doing
this with an LLM call would double per-turn cost on a free-models bot.

Tiers:
  S   tiny / chat / single tool   (smallest free models, low context)
  M   default                      (mid-tier free models)
  L   research / multi-step       (largest free models + extended turns)

Override prefixes:
  /quick <text>   → S, prefix stripped from message
  /deep  <text>   → L, prefix stripped from message
"""
from __future__ import annotations

import re

# Heuristics tuned for an autonomous Telegram bot. Kept conservative —
# misclassifying M as L only costs free-model latency; misclassifying L as M
# is the painful failure mode (5 turns isn't enough for real research).
_DEEP_RX = re.compile(
    r"\b("
    r"research|deep[\s-]?dive|comprehensive|investigat|"
    # "analyze X and Y" / "compare X with Y" — allow 1-6 words between
    r"analyze\s+(?:\w+\s+){1,6}and\b|"
    r"compare\s+(?:\w+\s+){1,6}(?:and|with|to|vs)\b|"
    r"find\s+(?:\w+\s+){1,4}(?:and|then)\b|"
    r"find\s+(?:me\s+)?\d+\s+(?:articles?|sources?|stocks?|tokens?|posts?|tweets?|papers?)|"
    r"summariz\w*\s+(?:multiple|all|several|each)|"
    r"cross[\s-]?reference|deeply\s+analyze"
    r")",
    re.IGNORECASE,
)

# Short, single-intent messages route to S. Length cap matters — "calc me a
# table of revenue and growth and..." starts with "calc" but is not S work.
_QUICK_RX = re.compile(
    r"\b("
    r"hi|hello|hey|thanks?|"
    r"good\s+(?:morning|afternoon|evening|night)|"
    r"what(?:\s+is|'s)?\s+(?:the\s+)?(?:time|date|weather)|"
    r"calc(?:ulate)?|"
    r"\d+\s*[+\-*/x×]\s*\d+"
    r")\b",
    re.IGNORECASE,
)

_QUICK_MAX_WORDS = 8


def classify(user_message: str) -> tuple[str, str]:
    """Return ``(tier, cleaned_message)``.

    The cleaned message has any explicit tier-override prefix removed so the
    LLM doesn't see prompt noise like ``/deep`` in its conversation history.
    """
    msg = (user_message or "").strip()
    if not msg:
        return ("M", msg)

    lower = msg.lower()
    if lower.startswith("/deep ") or lower == "/deep":
        return ("L", msg[len("/deep"):].lstrip())
    if lower.startswith("/quick ") or lower == "/quick":
        return ("S", msg[len("/quick"):].lstrip())

    if _DEEP_RX.search(msg):
        return ("L", msg)
    if _QUICK_RX.search(msg) and len(msg.split()) <= _QUICK_MAX_WORDS:
        return ("S", msg)
    return ("M", msg)
