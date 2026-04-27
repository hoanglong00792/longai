#!/usr/bin/env bash
#
# scripts/refresh_models.sh — query OpenRouter for current free tool-capable
# models and rewrite the model-chain block(s) in config.example.toml.
#
# Usage:
#   bash scripts/refresh_models.sh                    # legacy flat models = [...]
#   bash scripts/refresh_models.sh --tiered           # tiered [models.tier_S/M/L] + [models.fallback]
#   bash scripts/refresh_models.sh --user-config      # also update ~/.longai/config.toml
#   bash scripts/refresh_models.sh --dry-run          # print, don't write
#
# Legacy mode (default):
#   1. GET https://openrouter.ai/api/v1/models
#   2. Filter to slugs where pricing.prompt == "0" AND "tools" in supported_parameters
#   3. Sort by Doyen's heuristic ordering, append paid Gemma-4-26b floor
#   4. Replace the `models = [...]` block
#
# Tiered mode (--tiered):
#   1. Same fetch + filter
#   2. Auto-classify each slug into S/M/L using the parameter hint in the
#      slug ("9b", "70b", "1t") with context_length as fallback
#   3. Apply user overrides + skip list from ~/.longai/model_tiers.local.toml
#      (copy the repo template `model_tiers.example.toml` to enable)
#   4. Show tier breakdown + diff vs the previous classification + action
#      items (newly auto-classified slugs that may want overrides)
#   5. Replace any existing legacy `models = [...]` AND existing
#      [models.tier_*] / [models.fallback] tables with the new tiered blocks
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
TIERED=0

while [ $# -gt 0 ]; do
  case "$1" in
    --user-config) UPDATE_USER=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --tiered) TIERED=1; shift ;;
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

# Fetch ALL relevant fields once. Output: JSON list of [slug, ctx_len].
# Both legacy and tiered modes consume this; legacy projects to slugs only.
MODELS_RAW="$($ARCH_PREFIX "${PYBIN}" - <<'PY'
"""Fetch + filter free tool-capable models. Return [[slug, ctx_len], ...]."""
import json
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
    ctx = int(m.get("context_length") or
              (m.get("top_provider") or {}).get("context_length") or 0)
    free_tool.append([m["id"], ctx])

# Doyen's curated order — preferred families first, slow models last.
# Used by legacy mode; tiered mode classifies and orders within tier.
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

# Drop skips, then sort: PRIORITY first, then alphabetical
ctx_by_id = {s: c for s, c in free_tool}
ordered_ids = []
seen = set()
for s in PRIORITY:
    if s in ctx_by_id and s not in SKIP:
        ordered_ids.append(s); seen.add(s)
for s in sorted(ctx_by_id):
    if s not in seen and s not in SKIP:
        ordered_ids.append(s); seen.add(s)
PAID_FLOOR = "google/gemma-4-26b-a4b-it"
ordered_ids.append(PAID_FLOOR)
ctx_by_id[PAID_FLOOR] = ctx_by_id.get(PAID_FLOOR, 0)

print(json.dumps([[s, ctx_by_id.get(s, 0)] for s in ordered_ids]))
PY
)"

# Project to flat slug list for legacy formatting (preserves existing JSON shape)
MODELS_LIST="$($ARCH_PREFIX "${PYBIN}" -c \
  'import json,sys; print(json.dumps([s for s,_ in json.loads(sys.argv[1])]))' \
  "${MODELS_RAW}")"

COUNT=$(echo "${MODELS_LIST}" | $ARCH_PREFIX "${PYBIN}" -c "import json,sys; print(len(json.loads(sys.stdin.read())))")
echo "==> found ${COUNT} models (${COUNT} - 1 free + 1 paid floor)"

# ── Build TOML output ────────────────────────────────────────────────────
# Two shapes depending on mode:
#   legacy:  TOML_BLOCK = `models = [...]` (single array literal)
#   tiered:  TOML_BLOCK = four tables ([models.tier_S/M/L] + [models.fallback])

if [ "${TIERED}" = "1" ]; then
    # Use the user's config to seed `previous_chains` for the diff. Falls
    # back to config.example.toml if no user config exists yet.
    PREV_SOURCE=""
    if [ -f "${HOME}/.longai/config.toml" ]; then
        PREV_SOURCE="${HOME}/.longai/config.toml"
    elif [ -f "config.example.toml" ]; then
        PREV_SOURCE="config.example.toml"
    fi
    echo
    TOML_BLOCK="$($ARCH_PREFIX "${PYBIN}" - "${MODELS_RAW}" "${PREV_SOURCE}" <<'PY'
"""Classify rich model list into tier chains + render TOML blocks."""
import json
import sys
import tomllib
from pathlib import Path

# Make `src/longai/...` importable when running from the repo root
sys.path.insert(0, "src")

from longai.tier_auto_classifier import classify_models, diff_chains  # noqa: E402

raw_json, prev_source = sys.argv[1], sys.argv[2]
rich = json.loads(raw_json)  # [[slug, ctx_len], ...]
pairs = [(s, int(c)) for s, c in rich]

previous = None
if prev_source and Path(prev_source).exists():
    try:
        cfg = tomllib.loads(Path(prev_source).read_text())
        models_node = cfg.get("models")
        if isinstance(models_node, dict):
            previous = {
                "S": list((models_node.get("tier_S") or {}).get("chain", []) or []),
                "M": list((models_node.get("tier_M") or {}).get("chain", []) or []),
                "L": list((models_node.get("tier_L") or {}).get("chain", []) or []),
                "fallback": list((models_node.get("fallback") or {}).get("chain", []) or []),
            }
    except Exception:
        previous = None

result = classify_models(pairs, previous_chains=previous)
chains = result.tier_chains

# Print breakdown to STDERR so STDOUT stays clean for capture
def err(s=""): print(s, file=sys.stderr)

err()
err("==> tier classification:")
for tier in ("S", "M", "L", "fallback"):
    n = len(chains[tier])
    err(f"    [{tier:<8}]: {n}")

err()
err("==> diff vs previous:")
for line in diff_chains(previous, chains):
    err(line)

if result.skipped:
    err()
    err(f"==> skipped (from override file): {', '.join(result.skipped)}")
if result.overrides_applied:
    err()
    err(f"==> overrides applied: {len(result.overrides_applied)}")
    for slug, tier in sorted(result.overrides_applied.items()):
        err(f"    {tier:<8} {slug}")
if result.auto_classified_new:
    err()
    err("==> action items — newly auto-classified slugs:")
    err("    review and add to ~/.longai/model_tiers.local.toml [overrides]")
    err("    if the auto-pick is wrong:")
    for slug, tier in result.auto_classified_new:
        err(f"      {tier:<8} {slug}  (auto)")

# Render the four tables
out: list[str] = []
out.append("# Tier-aware model chains. Refreshed by")
out.append("# `bash scripts/refresh_models.sh --tiered`. Manual overrides go in")
out.append("# ~/.longai/model_tiers.local.toml (see model_tiers.example.toml).")
for tier in ("S", "M", "L"):
    out.append("")
    out.append(f"[models.tier_{tier}]")
    out.append("chain = [")
    for s in chains[tier]:
        out.append(f'    "{s}",')
    out.append("]")
out.append("")
out.append("[models.fallback]")
out.append("chain = [")
for s in chains["fallback"]:
    out.append(f'    "{s}",')
out.append("]")
print("\n".join(out))
PY
)"
else
    # Legacy: single `models = [...]` array literal
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
fi

echo
if [ "${TIERED}" = "1" ]; then
    echo "==> new tiered models blocks:"
else
    echo "==> new models block:"
fi
echo "${TOML_BLOCK}"

if [ "${DRY_RUN}" = "1" ]; then
    echo
    echo "==> dry-run mode; not writing"
    exit 0
fi

# Replace the models block(s) in a TOML file. In tiered mode, also strip
# any pre-existing [models.tier_*] tables and the legacy `models = [...]`
# array (so re-running this script is idempotent regardless of the file's
# previous shape).
write_models_block() {
    local target="$1"
    if [ ! -f "${target}" ]; then
        echo "  skipping ${target} (not found)"
        return
    fi
    $ARCH_PREFIX "${PYBIN}" - "${target}" "${TOML_BLOCK}" "${TIERED}" <<'PY'
import re
import sys

target, new_block, tiered = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(target).read()

if tiered == "1":
    # Remove legacy `models = [ ... ]` (multi-line array, no nested brackets).
    # Anchor to ^models so `# models = [...]` in comments isn't matched.
    text = re.sub(
        r'^models\s*=\s*\[[^\]]*\]\s*\n',
        '',
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    # Strip our marker comment block so re-running doesn't accumulate copies
    # of it. Fingerprint: a 3-line comment starting with our exact phrase.
    text = re.sub(
        r'^# Tier-aware model chains\. Refreshed by\n'
        r'# `bash scripts/refresh_models\.sh --tiered`[^\n]*\n'
        r'# ~/\.longai/model_tiers\.local\.toml[^\n]*\n',
        '',
        text,
        flags=re.MULTILINE,
    )
    # Remove [models.X] tables — header through next [...] header or EOF.
    # CRITICAL: anchor headers to ^ so commented examples like
    # `# [models.tier_S]` are NOT matched and chewed through.
    for header in ('[models.tier_S]', '[models.tier_M]', '[models.tier_L]',
                   '[models.fallback]'):
        pat = r'^' + re.escape(header) + r'\s*\n.*?(?=^\[|\Z)'
        text = re.sub(pat, '', text, flags=re.MULTILINE | re.DOTALL)
    # Cosmetic: collapse runs of blank lines, then append the new block
    text = re.sub(r'\n{3,}', '\n\n', text).rstrip() + '\n\n' + new_block.strip() + '\n'
    open(target, 'w').write(text)
    print(f"  wrote (tiered) {target}")
else:
    pattern = re.compile(r'^models\s*=\s*\[[^\]]*\]', re.MULTILINE | re.DOTALL)
    new_text, n = pattern.subn(new_block.strip(), text, count=1)
    if n == 0:
        print(f"  WARNING: no 'models = [...]' block found in {target}; appending")
        new_text = text.rstrip() + '\n\n' + new_block.strip() + '\n'
    open(target, 'w').write(new_text)
    print(f"  wrote {target}")
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
