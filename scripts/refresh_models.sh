#!/usr/bin/env bash
#
# scripts/refresh_models.sh — query OpenRouter for current free tool-capable
# models and rewrite the `models = [...]` block in config.example.toml.
#
# Usage:
#   bash scripts/refresh_models.sh                    # update config.example.toml
#   bash scripts/refresh_models.sh --user-config      # also update ~/.longai/config.toml
#   bash scripts/refresh_models.sh --dry-run          # print, don't write
#
# What it does:
#   1. GET https://openrouter.ai/api/v1/models
#   2. Filter to slugs where pricing.prompt == "0" AND "tools" in supported_parameters
#   3. Sort by Doyen's heuristic ordering (Gemma first, then capable mid-tier,
#      then small/fast fallbacks, then meta-router, then paid floor)
#   4. Append paid Gemma-4-26b as the floor
#   5. Rewrite the `models = [...]` block in config.example.toml (and
#      optionally ~/.longai/config.toml with --user-config)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

ARCH_PREFIX=""
if [ "$(uname)" = "Darwin" ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
    ARCH_PREFIX="arch -arm64"
fi
PYBIN="${REPO_ROOT}/.venv/bin/python"

UPDATE_USER=0
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --user-config) UPDATE_USER=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ ! -x "${PYBIN}" ]; then
    echo "ERROR: .venv/bin/python not found. Run scripts/setup_venv.sh first." >&2
    exit 2
fi

echo "==> querying OpenRouter for free tool-capable models..."

MODELS_LIST="$($ARCH_PREFIX "${PYBIN}" - <<'PY'
"""Fetch + filter + order free tool-capable models from OpenRouter."""
import json
import sys

import httpx

resp = httpx.get("https://openrouter.ai/api/v1/models", timeout=15.0)
resp.raise_for_status()
data = resp.json()

free_tool = []
for m in data.get("data", []):
    pricing = m.get("pricing", {})
    if pricing.get("prompt") != "0":
        continue
    supported = m.get("supported_parameters") or []
    if "tools" not in supported:
        continue
    free_tool.append(m["id"])

# Doyen's curated order — preferred families first, slow models last.
# Anything not in this priority list goes to the end (alphabetical).
PRIORITY = [
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
    "minimax/minimax-m2.5:free",
    "tencent/hy3-preview:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "inclusionai/ling-2.6-flash:free",
    "inclusionai/ling-2.6-1t:free",
    "liquid/lfm-2.5-1.2b-instruct:free",
    "liquid/lfm-2.5-1.2b-thinking:free",
    "openrouter/free",
]
SKIP = {
    # OCR-specialized — claims tool-call support but performs poorly on agent loops.
    "baidu/qianfan-ocr-fast:free",
}

ordered = []
seen = set()
for m in PRIORITY:
    if m in free_tool and m not in SKIP:
        ordered.append(m); seen.add(m)
for m in sorted(free_tool):
    if m not in seen and m not in SKIP:
        ordered.append(m); seen.add(m)

# Append paid floor
PAID_FLOOR = "google/gemma-4-26b-a4b-it"
ordered.append(PAID_FLOOR)

print(json.dumps(ordered))
PY
)"

COUNT=$(echo "${MODELS_LIST}" | $ARCH_PREFIX "${PYBIN}" -c "import json,sys; print(len(json.loads(sys.stdin.read())))")
echo "==> found ${COUNT} models (${COUNT} - 1 free + 1 paid floor)"

# Format as TOML array literal block, with comment markers for the section
TOML_BLOCK="$($ARCH_PREFIX "${PYBIN}" - <<PY
import json
slugs = json.loads('''${MODELS_LIST}''')
lines = ['models = [']
for s in slugs[:-1]:
    lines.append(f'    "{s}",')
lines.append(f'    # PAID FLOOR — only used when EVERY free model above is on cooldown')
lines.append(f'    "{slugs[-1]}",')
lines.append(']')
print('\n'.join(lines))
PY
)"

echo
echo "==> new models block:"
echo "${TOML_BLOCK}"

if [ "${DRY_RUN}" = "1" ]; then
    echo
    echo "==> dry-run mode; not writing"
    exit 0
fi

# Replace the models = [ ... ] block in a TOML file using Python
write_models_block() {
    local target="$1"
    if [ ! -f "${target}" ]; then
        echo "  skipping ${target} (not found)"
        return
    fi
    $ARCH_PREFIX "${PYBIN}" - <<PY
import re
path = "${target}"
new_block = '''${TOML_BLOCK}'''.strip()

text = open(path).read()
# Match: optional comment block immediately preceding `models = [...]`
# Just replace the array assignment itself; preserve any comments above.
pattern = re.compile(r'^models\s*=\s*\[[^\]]*\]', re.MULTILINE | re.DOTALL)
new_text, n = pattern.subn(new_block, text, count=1)
if n == 0:
    print(f"  WARNING: no 'models = [...]' block found in {path}; appending")
    new_text = text.rstrip() + '\n\n' + new_block + '\n'
open(path, 'w').write(new_text)
print(f"  wrote {path}")
PY
}

echo
echo "==> writing config.example.toml"
write_models_block "config.example.toml"

if [ "${UPDATE_USER}" = "1" ]; then
    USER_CFG="${HOME}/.longai/config.toml"
    echo "==> writing ${USER_CFG}"
    write_models_block "${USER_CFG}"
fi

echo
echo "==> done. Verify with: uv run longai dryrun"
echo "==> Models list updated in:"
echo "      - config.example.toml"
[ "${UPDATE_USER}" = "1" ] && echo "      - ${USER_CFG}"
