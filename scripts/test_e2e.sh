#!/usr/bin/env bash
# Runs golden prompts against the llmstub (or live OR if LONGAI_LIVE=1).
set -euo pipefail

GOLDEN_DIR="tests/golden"
PORT=9999

if [ "${LONGAI_LIVE:-0}" != "1" ]; then
    python scripts/llmstub.py $PORT &
    STUB_PID=$!
    trap "kill $STUB_PID 2>/dev/null || true" EXIT
    export OPENROUTER_BASE_URL="http://127.0.0.1:$PORT/v1"
    export OPENROUTER_API_KEY="stub"
    sleep 1
fi

PASS=0
FAIL=0
for f in "$GOLDEN_DIR"/*.jsonl; do
    [ -e "$f" ] || continue
    while IFS= read -r line; do
        prompt=$(echo "$line" | python -c "import sys,json; print(json.loads(sys.stdin.read())['prompt'])")
        expected=$(echo "$line" | python -c "import sys,json; print(json.loads(sys.stdin.read())['expected_stopped'])")
        out=$(python -m longai run --user-id -1 -- "$prompt" 2>&1 || true)
        stopped=$(echo "$out" | python -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('stopped',''))")
        if [ "$stopped" = "$expected" ]; then
            PASS=$((PASS+1))
        else
            FAIL=$((FAIL+1))
            echo "FAIL: $prompt → expected $expected, got $stopped"
        fi
    done < "$f"
done

echo "PASS=$PASS FAIL=$FAIL"
exit $FAIL
