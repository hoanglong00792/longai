#!/usr/bin/env bash
# I3 enforcement: only budget_guard.py imports openai or hand-rolls openrouter HTTP

set -euo pipefail

VIOLATIONS=$(grep -rn -E 'openai\.AsyncOpenAI|openai\.OpenAI|httpx.*openrouter' \
    src/longai/ src/longai_mcps/ \
    --exclude-dir=__pycache__ \
    --exclude=budget_guard.py \
    || true)

if [ -n "$VIOLATIONS" ]; then
    echo "❌ I3 VIOLATION: only src/longai/budget_guard.py may import openai or hand-roll openrouter HTTP."
    echo "$VIOLATIONS"
    exit 1
fi

echo "✅ I3 enforced: budget_guard.py is the only OpenRouter chokepoint."
