#!/usr/bin/env bash
#
# scripts/test_all.sh — single entry point for the full test suite:
#   1. pytest unit tests (~3s, no network)
#   2. live feature scripts via test_features.sh
#      (test_dryrun + test_models_cache + test_learn + test_chat_streaming
#       + live_replay tier 1)
#
# Exits non-zero if ANY layer failed. Prints a combined summary.
#
# Usage:
#   OPENROUTER_API_KEY_FILE=~/.../openrouter.env ./scripts/test_all.sh
#   ./scripts/test_all.sh --skip-live           # pytest + offline scripts
#   ./scripts/test_all.sh --no-pytest           # just the live tests
#   ./scripts/test_all.sh --skip-live --no-pytest  # offline scripts only
#   ./scripts/test_all.sh --skip-replay         # everything except the
#                                                 long live_replay sweep

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
unset VIRTUAL_ENV

NO_PYTEST=0
PASSTHROUGH=()
while [ $# -gt 0 ]; do
    case "$1" in
        --no-pytest)   NO_PYTEST=1; shift ;;
        --skip-live)   PASSTHROUGH+=("--skip-live"); shift ;;
        --skip-replay) PASSTHROUGH+=("--skip-replay"); shift ;;
        -h|--help)     sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

declare -a RESULTS=()
ANY_FAIL=0

# ── Layer 1: pytest unit tests ─────────────────────────────────────
if [ "${NO_PYTEST}" = "0" ]; then
    echo "============================================================"
    echo "  LAYER 1 — pytest unit tests"
    echo "============================================================"
    t0=$(date +%s)
    # Pytest unit tests must NOT see live API keys — at least one test
    # (test_dryrun_panics_without_api_key) asserts behavior when neither
    # is set. Strip both vars for this layer only; the live layer below
    # still inherits them from the parent shell.
    if env -u OPENROUTER_API_KEY -u OPENROUTER_API_KEY_FILE \
            $ARCH_PREFIX "${PYBIN}" -m pytest tests/ -q --ignore=tests/e2e \
            > /tmp/test_all_pytest.out 2>&1; then
        t1=$(date +%s)
        # Extract the trailing "N passed in Xs" line for the summary
        SUMMARY="$(grep -oE '[0-9]+ passed.*' /tmp/test_all_pytest.out | tail -1 || echo 'unknown')"
        RESULTS+=("PASS  pytest unit tests  ($((t1 - t0))s, ${SUMMARY})")
        echo "    PASS: ${SUMMARY}"
    else
        t1=$(date +%s)
        RESULTS+=("FAIL  pytest unit tests  ($((t1 - t0))s)")
        ANY_FAIL=1
        echo "    FAIL: pytest unit tests (see /tmp/test_all_pytest.out)"
        tail -20 /tmp/test_all_pytest.out | sed 's/^/        /'
    fi
    echo
fi

# ── Layer 2: live feature scripts (delegated to test_features.sh) ──
echo "============================================================"
echo "  LAYER 2 — live feature scripts"
echo "============================================================"
FEATURES_SCRIPT="${SCRIPT_DIR}/test_features.sh"
if [ ! -x "${FEATURES_SCRIPT}" ]; then
    RESULTS+=("MISS  test_features.sh")
    ANY_FAIL=1
else
    t0=$(date +%s)
    if "${FEATURES_SCRIPT}" "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}" \
            > /tmp/test_all_features.out 2>&1; then
        t1=$(date +%s)
        # Pull the test_features.sh tail for context
        FEATURE_TAIL="$(grep -E '^  (PASS|FAIL|MISS)' /tmp/test_all_features.out || true)"
        RESULTS+=("PASS  test_features.sh  ($((t1 - t0))s)")
        echo "    PASS — sub-results:"
        echo "${FEATURE_TAIL}" | sed 's/^/    /'
    else
        t1=$(date +%s)
        RESULTS+=("FAIL  test_features.sh  ($((t1 - t0))s)")
        ANY_FAIL=1
        echo "    FAIL — sub-results:"
        grep -E '^  (PASS|FAIL|MISS)' /tmp/test_all_features.out | sed 's/^/    /' || true
        echo "    (full log: /tmp/test_all_features.out)"
    fi
fi

# ── Combined summary ────────────────────────────────────────────────
echo
echo "============================================================"
echo "  COMBINED TEST SUMMARY"
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
echo "  layers pass=${PASS_N}  fail=${FAIL_N}  miss=${MISS_N}"
echo "============================================================"

exit "${ANY_FAIL}"
