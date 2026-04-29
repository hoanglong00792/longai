#!/usr/bin/env bash
#
# scripts/test_features.sh — master harness running every live feature test.
#
# Order is cheap-to-expensive: dryrun (~1s) → models_cache (~1s) → learn
# (~10-30s, 1 LLM call) → chat_streaming (~5-30s, 1 LLM call) → live_replay
# tier 1 (~30-90s, 5 LLM calls).
#
# Exits non-zero if any sub-test failed. Prints a summary table.
#
# Usage:
#   OPENROUTER_API_KEY_FILE=~/.../openrouter.env ./scripts/test_features.sh
#   OPENROUTER_API_KEY_FILE=...  ./scripts/test_features.sh --skip-live   # offline-only
#   OPENROUTER_API_KEY_FILE=...  ./scripts/test_features.sh --skip-replay # skip live_replay

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

SKIP_LIVE=0
SKIP_REPLAY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --skip-live)   SKIP_LIVE=1; shift ;;
        --skip-replay) SKIP_REPLAY=1; shift ;;
        -h|--help)     sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# ── Cheap (no LLM) tests run unconditionally ────────────────────────
declare -a CHEAP_TESTS=(
    "test_dryrun.sh"
    "test_models_cache.sh"
)

# ── Live tests skipped when --skip-live ─────────────────────────────
declare -a LIVE_TESTS=(
    "test_learn.sh"
    "test_chat_streaming.sh"
)

declare -a RESULTS=()

run_test() {
    local name="$1"
    local path="${SCRIPT_DIR}/${name}"
    if [ ! -x "${path}" ]; then
        RESULTS+=("MISS  ${name}")
        return
    fi
    local t0 t1
    t0=$(date +%s)
    if "${path}" > "/tmp/${name}.out" 2>&1; then
        t1=$(date +%s)
        RESULTS+=("PASS  ${name}  ($((t1 - t0))s)")
        echo "    PASS: ${name}"
    else
        t1=$(date +%s)
        RESULTS+=("FAIL  ${name}  ($((t1 - t0))s)")
        echo "    FAIL: ${name}  (see /tmp/${name}.out)"
        tail -10 "/tmp/${name}.out" | sed 's/^/        /'
    fi
}

echo "==> cheap (no-LLM) tests"
for t in "${CHEAP_TESTS[@]}"; do run_test "${t}"; done

if [ "${SKIP_LIVE}" = "1" ]; then
    echo
    echo "==> --skip-live: skipping LLM-dependent tests"
else
    if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -z "${OPENROUTER_API_KEY_FILE:-}" ]; then
        echo "ERROR: live tests require OPENROUTER_API_KEY or OPENROUTER_API_KEY_FILE." >&2
        echo "       Pass --skip-live to skip LLM-dependent tests." >&2
        exit 2
    fi
    echo
    echo "==> live (LLM) tests"
    for t in "${LIVE_TESTS[@]}"; do run_test "${t}"; done

    if [ "${SKIP_REPLAY}" = "1" ]; then
        echo
        echo "==> --skip-replay: skipping live_replay sweep"
    else
        echo
        echo "==> live_replay tier 1 sweep"
        REPLAY="${SCRIPT_DIR}/live_replay.sh"
        if [ -x "${REPLAY}" ]; then
            local_t0=$(date +%s)
            if "${REPLAY}" --tier 1 --throttle 5 --reset-cooldowns \
                    > /tmp/live_replay.out 2>&1; then
                local_t1=$(date +%s)
                RESULTS+=("PASS  live_replay --tier 1  ($((local_t1 - local_t0))s)")
                echo "    PASS: live_replay --tier 1"
                grep -E "==>" /tmp/live_replay.out | tail -3 | sed 's/^/        /'
            else
                local_t1=$(date +%s)
                RESULTS+=("FAIL  live_replay --tier 1  ($((local_t1 - local_t0))s)")
                echo "    FAIL: live_replay --tier 1  (see /tmp/live_replay.out)"
                tail -10 /tmp/live_replay.out | sed 's/^/        /'
            fi
        else
            RESULTS+=("MISS  live_replay.sh")
        fi
    fi
fi

# ── Summary ────────────────────────────────────────────────────────
echo
echo "============================================================"
echo "  TEST SUMMARY"
echo "============================================================"
PASS_N=0; FAIL_N=0; MISS_N=0
for r in "${RESULTS[@]}"; do
    echo "  ${r}"
    case "${r}" in
        PASS*) PASS_N=$((PASS_N + 1)) ;;
        FAIL*) FAIL_N=$((FAIL_N + 1)) ;;
        MISS*) MISS_N=$((MISS_N + 1)) ;;
    esac
done
echo "============================================================"
echo "  pass=${PASS_N}  fail=${FAIL_N}  miss=${MISS_N}"
echo "============================================================"

if [ "${FAIL_N}" -gt 0 ]; then
    exit 1
fi
exit 0
