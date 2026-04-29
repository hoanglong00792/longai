#!/usr/bin/env bash
#
# scripts/test_dryrun.sh — smoke-test `longai dryrun`.
#
# Verifies the config layer reads cleanly and reports the expected
# sections. No LLM calls, no network. Uses a fake key if no real one is
# set — `dryrun` only checks the key is non-empty.
#
# Exits non-zero on any failed assertion.

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

# Fake key is fine — dryrun only checks the env var is set, never calls the API.
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-fake_dryrun_only}"

echo "==> [1/3] running longai dryrun"
OUT="$($ARCH_PREFIX "${PYBIN}" -m longai dryrun 2>&1)"
echo "${OUT}" | sed 's/^/    /'

echo
echo "==> [2/3] checking required sections"
fail() { echo "FAIL: $1" >&2; exit 1; }

echo "${OUT}" | grep -q "config OK"     || fail "missing 'config OK' line"
echo "${OUT}" | grep -qE "models|tier_" || fail "missing models/tier breakdown"
echo "${OUT}" | grep -q "caps:"         || fail "missing 'caps:' line"
echo "${OUT}" | grep -q "db:"           || fail "missing 'db:' line"
echo "${OUT}" | grep -q "mcp config:"   || fail "missing 'mcp config:' line"
echo "    PASS: all required sections present"

echo
echo "==> [3/3] checking exit code"
$ARCH_PREFIX "${PYBIN}" -m longai dryrun > /dev/null 2>&1 || fail "non-zero exit code"
echo "    PASS: exit 0"

echo
echo "==> ALL PASS — longai dryrun reads config and reports cleanly"
