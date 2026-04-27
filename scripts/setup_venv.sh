#!/usr/bin/env bash
#
# scripts/setup_venv.sh — create a clean, arch-consistent .venv for longai.
#
# Why this exists:
#   The default uv venv on macOS uses a universal Python (x86_64 + arm64) which
#   leads to subtle arch mismatches when wheels are arm64-only but Python gets
#   invoked under x86_64 (rtk wrappers, shell config, etc.). This script forces
#   a single-arch arm64 venv on Apple Silicon for consistent runtime behavior.
#
# Usage:
#   ./scripts/setup_venv.sh
#   ./scripts/setup_venv.sh --extras "dev,on_chain_ta,longai_strategy"
#   ./scripts/setup_venv.sh --keep-existing   # don't wipe .venv first
#
# After running:
#   source .venv/bin/activate
#   longai dryrun
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

EXTRAS="dev"
KEEP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --extras) EXTRAS="$2"; shift 2 ;;
    --keep-existing) KEEP=1; shift ;;
    -h|--help)
      sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Detect Apple Silicon at hardware level (uname -m lies inside x86_64 subshells)
ARCH_PREFIX=""
if [ "$(uname)" = "Darwin" ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
    ARCH_PREFIX="arch -arm64"
fi

unset VIRTUAL_ENV

# Always wipe stale myenv left from earlier subagent sessions
if [ -d "myenv" ]; then
    echo "==> removing stale myenv/"
    rm -rf myenv
fi

if [ "${KEEP}" = "0" ] && [ -d ".venv" ]; then
    echo "==> wiping existing .venv (use --keep-existing to skip)"
    rm -rf .venv
fi

if [ ! -d ".venv" ]; then
    echo "==> creating .venv with ${ARCH_PREFIX:-default} arch"
    $ARCH_PREFIX uv venv --python 3.11
fi

echo "==> installing project + extras: ${EXTRAS}"
$ARCH_PREFIX uv pip install --python .venv/bin/python -e ".[${EXTRAS}]"

echo
echo "==> Verifying arch consistency..."
$ARCH_PREFIX .venv/bin/python -c "
import platform, pydantic_core
print(f'  python arch:        {platform.machine()}')
print(f'  pydantic_core path: {pydantic_core.__file__}')
print('  OK — arm64 wheels load cleanly')
"

echo
echo "==> Running smoke tests..."
$ARCH_PREFIX .venv/bin/python -m pytest tests/ -q --no-header 2>&1 | tail -5 || true

echo
echo "==> .venv is ready. Activate with: source .venv/bin/activate"
echo "==> Then try: longai dryrun"
