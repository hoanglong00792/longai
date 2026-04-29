#!/usr/bin/env bash
#
# scripts/test_models_cache.sh — smoke-test `longai refresh --show`.
#
# Verifies the on-disk free-models cache is parseable and contains at
# least one model entry. No network call. If the cache file does not
# exist, the test exits with a clear WARN+exit 0 (the cache is created
# on the first real `longai refresh` — not having it yet is fine).

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

# Fake key — refresh --show is a local read; never touches the network.
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-fake_show_only}"

CACHE_PATH="${HOME}/.longai/models_cache.json"

echo "==> [1/3] checking cache existence"
if [ ! -f "${CACHE_PATH}" ]; then
    echo "    WARN: ${CACHE_PATH} does not exist yet."
    echo "          Run 'longai refresh' (without --show) to populate it."
    echo "          Skipping content validation; exit 0."
    exit 0
fi
echo "    PASS: ${CACHE_PATH} exists"

echo
echo "==> [2/3] running longai refresh --show"
OUT="$($ARCH_PREFIX "${PYBIN}" -m longai refresh --show 2>&1)"
echo "${OUT}" | sed 's/^/    /' | head -20

echo
echo "==> [3/3] checking output shape"
fail() { echo "FAIL: $1" >&2; exit 1; }

echo "${OUT}" | grep -q "cache:"        || fail "missing 'cache:' header"
echo "${OUT}" | grep -q "free models:"  || fail "missing 'free models:' count"
echo "${OUT}" | grep -q "chain"         || fail "missing 'chain' section"

# Free model count must be ≥ 1
COUNT="$(echo "${OUT}" | grep -oE 'free models: [0-9]+' | grep -oE '[0-9]+' | head -1 || echo 0)"
if [ "${COUNT}" -lt 1 ]; then
    fail "free model count = ${COUNT} (expected ≥ 1)"
fi
echo "    PASS: ${COUNT} free model(s) in cache"

echo
echo "==> ALL PASS — models cache is readable and non-empty"
