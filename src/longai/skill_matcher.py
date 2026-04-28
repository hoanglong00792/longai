"""Match the user's prompt against the available skill catalog so we only
inject relevant skills into the system prompt — not all 60.

Phase 6 design (per LATENCY_PHASES.md):
- Strict English-only stopwords (preserve domain words like "swap", "buy").
- Token overlap scoring; min_score=2 to filter accidental single-word hits.
- Empty result is fine: cli.py still adds a trailing list_skills hint so
  the model can self-discover when it senses a need.
"""
from __future__ import annotations

import re

# Strict English stopwords. We deliberately keep domain words ("swap",
# "buy", "trade", "send", "show", "check") because they are exactly the
# trigger keywords skills index by. Erring on recall over precision.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "then", "else",
    "of", "to", "in", "on", "at", "by", "for", "with", "from", "into",
    "is", "am", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "have", "has", "had",
    "i", "me", "my", "you", "your", "we", "our", "us",
    "this", "that", "these", "those", "it", "its",
    "what", "when", "where", "why", "how", "which",
    "can", "could", "would", "should", "may", "might", "will", "shall",
    "as", "than", "so", "too", "also", "just", "only", "very",
    "no", "not", "yes", "ok", "okay",
})

# Word-character regex split. Drops punctuation, keeps alphanumerics + _.
# Min length 3 filters out noise like "is" / "to" / "go" without being
# domain-specific.
_TOKEN_RE = re.compile(r"\w+")
_MIN_TOKEN_LEN = 3


def _tokens(text: str) -> set[str]:
    """Tokenize text → lowercase word set, stopwords + short words removed."""
    return {
        t for t in (m.group().lower() for m in _TOKEN_RE.finditer(text or ""))
        if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS
    }


def match_skills(
    prompt: str,
    skills: list[dict],
    *,
    top_n: int = 8,
    min_score: int = 2,
) -> list[dict]:
    """Return up to ``top_n`` skills whose name+description share at least
    ``min_score`` tokens with ``prompt``. Sorted by score (descending),
    config order preserved on ties.

    Empty input → empty list. Empty prompt token set → empty list.
    """
    prompt_tokens = _tokens(prompt)
    if not prompt_tokens or not skills:
        return []
    scored: list[tuple[int, int, dict]] = []
    for idx, s in enumerate(skills):
        # Match against name + description so "swap-build" matches a prompt
        # mentioning "swap" via the name even if description is generic.
        skill_text = f"{s.get('name', '')} {s.get('description', '')}"
        skill_tokens = _tokens(skill_text)
        score = len(prompt_tokens & skill_tokens)
        if score >= min_score:
            # Negate score for sort (Python's tuple sort is ascending).
            scored.append((-score, idx, s))
    scored.sort()
    return [s for _, _, s in scored[:top_n]]
