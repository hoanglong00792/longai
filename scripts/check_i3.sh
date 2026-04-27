#!/usr/bin/env bash
# I3 enforcement: only budget_guard.py imports openai or hand-rolls openrouter
# CHAT-COMPLETION HTTP. The /api/v1/models metadata endpoint (used by
# models_cache.py for free-model discovery) is NOT chat completion and is
# explicitly allowed — it doesn't spend money and doesn't pass through caps.
#
# Rule: forbid OpenAI client construction OR direct hits to chat/completions.
# Allow: hits to /api/v1/models or other metadata endpoints (read-only, free).

set -euo pipefail

VIOLATIONS=$(grep -rn -E \
    'openai\.AsyncOpenAI|openai\.OpenAI|chat/completions' \
    src/longai/ src/longai_mcps/ \
    --exclude-dir=__pycache__ \
    --exclude=budget_guard.py \
    || true)

if [ -n "$VIOLATIONS" ]; then
    echo "❌ I3 VIOLATION: only src/longai/budget_guard.py may construct an OpenAI client or call chat/completions."
    echo "$VIOLATIONS"
    exit 1
fi

echo "✅ I3 enforced: budget_guard.py is the only chat-completion chokepoint."
echo "    (metadata endpoints like /api/v1/models are allowed elsewhere)"
