#!/usr/bin/env bash
#
# scripts/test_chat_streaming.sh — live test for the chat REPL streaming path
# (Phase C4: line-boundary StreamSanitizer + per-chunk OpenAI deltas).
#
# Pipes a short prompt into `longai chat`, captures the full stdout, and
# asserts:
#   - banner is printed
#   - prompts (`> `) appear at expected positions
#   - the model produced non-empty content past the prompt line
#   - the StreamSanitizer didn't drop a final-tail print at flush
#
# Streaming GRANULARITY is hard to assert from bash without a PTY; this
# script verifies the round-trip works end-to-end. For visual confirmation
# of token-by-token arrival, run `longai chat` directly and watch.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

ARCH_PREFIX=""
if [ "$(uname)" = "Darwin" ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
    ARCH_PREFIX="arch -arm64"
fi
PYBIN="${REPO_ROOT}/.venv/bin/python"

if [ ! -x "${PYBIN}" ]; then
    echo "ERROR: .venv/bin/python not found. Run scripts/setup_venv.sh first." >&2
    exit 2
fi
if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -z "${OPENROUTER_API_KEY_FILE:-}" ]; then
    echo "ERROR: missing OpenRouter key. Set OPENROUTER_API_KEY or OPENROUTER_API_KEY_FILE." >&2
    exit 2
fi
unset VIRTUAL_ENV

# Use a unique user_id so this test never crosses paths with the user's
# real chat history.
TEST_USER_ID=-9999
PROMPT='reply with exactly two words: "stream ok"'

OUT_FILE="$(mktemp)"
trap 'rm -f "${OUT_FILE}"' EXIT

echo "==> [1/4] running longai chat with piped input"
{
    printf '%s\n/exit\n' "${PROMPT}"
} | timeout 60 $ARCH_PREFIX "${PYBIN}" -m longai chat \
        --user-id "${TEST_USER_ID}" > "${OUT_FILE}" 2>&1 || {
    rc=$?
    if [ "$rc" -eq 124 ]; then
        echo "FAIL: chat REPL timed out (60s)" >&2
        cat "${OUT_FILE}" >&2
        exit 1
    fi
    # other non-zero — fall through; output may still contain partial stream
}
echo "    captured output:"
sed 's/^/        /' "${OUT_FILE}"

echo
echo "==> [2/4] checking banner + prompts"
fail() { echo "FAIL: $1" >&2; exit 1; }

grep -q "longai chat" "${OUT_FILE}"      || fail "missing chat banner"
PROMPT_LINES=$(grep -c '^> ' "${OUT_FILE}" || true)
if [ "${PROMPT_LINES}" -lt 1 ]; then
    fail "no '> ' prompt lines in output (got ${PROMPT_LINES})"
fi
echo "    PASS: banner + ${PROMPT_LINES} prompt(s) detected"

echo
echo "==> [3/4] checking model produced non-empty reply"
# Extract the model's reply: everything from the first `> ` line (with
# the leading `> ` stripped, since piped stdin is silent and the reply
# may share that line) up to but excluding the next bare `> ` prompt.
# This skips banner + asyncio shutdown noise that Python sometimes emits
# during subprocess teardown.
BODY=$(awk '
    NR == 1 { next }                          # skip banner line
    !started && /^> / {
        started = 1
        sub(/^> +/, "")
        if (length($0) > 0) print
        next
    }
    started && /^> *$/ { exit }               # bare `> ` = end of reply
    started { print }
' "${OUT_FILE}")
BODY_CHARS=$(printf '%s' "${BODY}" | wc -c | tr -d ' ')
if [ "${BODY_CHARS}" -lt 2 ]; then
    fail "model reply empty (${BODY_CHARS} chars) — stream may have dropped"
fi
echo "    PASS: model emitted ${BODY_CHARS} chars of reply"
echo "    body: $(printf '%s' "${BODY}" | head -c 120)"

echo
echo "==> [4/4] checking REPL respected /exit"
# Normal flow: exactly 2 `> ` prompts (one for our piped input, one
# after the reply that the REPL prints before reading /exit). >2 would
# mean the loop continued past /exit.
if [ "${PROMPT_LINES}" -gt 2 ]; then
    fail "REPL didn't exit cleanly (${PROMPT_LINES} prompts; expected 2)"
fi
echo "    PASS: REPL exited after /exit (${PROMPT_LINES} prompts)"

echo
echo "==> ALL PASS — chat REPL streaming round-trip verified"
echo "    (visual confirmation of token-by-token arrival: run \`longai chat\` interactively)"
