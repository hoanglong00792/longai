#!/usr/bin/env bash
#
# scripts/init_mcp.sh — generate ~/.longai/mcp.json from mcp.example.json,
# resolving placeholder paths to absolute paths for THIS machine.
#
# Usage:
#   bash scripts/init_mcp.sh                        # write ~/.longai/mcp.json
#   bash scripts/init_mcp.sh --force                # overwrite if it exists
#   bash scripts/init_mcp.sh --dest /path/mcp.json  # custom output path
#   bash scripts/init_mcp.sh --no-playwright        # skip Playwright (NOT recommended)
#
# What it does:
#   1. Verifies REQUIRED prerequisites (npx for Playwright, .venv/bin/python)
#   2. Resolves PLACEHOLDER_VENV_PYTHON  → ABS path to .venv/bin/python
#   3. Resolves PLACEHOLDER_DB_PATH      → ABS path to ~/.longai/state.db
#   4. Resolves PLACEHOLDER_SKILLS_*     → ABS paths to sibling skill repos
#                                          (entries dropped if a repo doesn't exist)
#   5. Strips _comment fields (clean JSON)
#   6. Writes to ~/.longai/mcp.json (or --dest)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HOME_DIR="$(cd "${HOME}" && pwd)"
EXAMPLE="${REPO_ROOT}/mcp.example.json"
DEST="${HOME_DIR}/.longai/mcp.json"
FORCE=0
SKIP_PLAYWRIGHT=0

while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --dest) DEST="$2"; shift 2 ;;
    --no-playwright) SKIP_PLAYWRIGHT=1; shift ;;
    -h|--help)
      sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> longai mcp.json initializer"
echo

# ---------------------------------------------------------------------------
# Step 1 — verify prerequisites
# ---------------------------------------------------------------------------

ERRORS=0

if [ ! -f "${EXAMPLE}" ]; then
    echo "❌ ${EXAMPLE} not found"
    ERRORS=$((ERRORS + 1))
fi

VENV_PY="${REPO_ROOT}/.venv/bin/python"
if [ ! -x "${VENV_PY}" ]; then
    echo "❌ .venv/bin/python not found at ${VENV_PY}"
    echo "   Fix: run 'bash scripts/setup_venv.sh' first"
    ERRORS=$((ERRORS + 1))
else
    echo "✅ venv python:    ${VENV_PY}"
fi

if [ "${SKIP_PLAYWRIGHT}" = "0" ]; then
    if ! command -v npx >/dev/null 2>&1; then
        echo "❌ npx not found in PATH"
        echo "   Playwright MCP REQUIRES Node.js 18+ with npx."
        echo "   Install from https://nodejs.org or via 'brew install node'."
        echo "   To proceed without Playwright, re-run with --no-playwright"
        echo "   (NOT recommended — Playwright is the default browser tool)."
        ERRORS=$((ERRORS + 1))
    else
        NODE_VERSION="$(node --version 2>/dev/null || echo unknown)"
        echo "✅ npx found:      $(which npx) (node ${NODE_VERSION})"
    fi
fi

if [ "${ERRORS}" -gt 0 ]; then
    echo
    echo "==> ${ERRORS} prerequisite check(s) failed. Fix and re-run."
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 2 — discover skill repo paths (siblings of repo root)
# ---------------------------------------------------------------------------

SKILLS_SHARED="$(cd "${REPO_ROOT}/.." && pwd)/longai-skills-shared"
SKILLS_PERSONAL="$(cd "${REPO_ROOT}/.." && pwd)/longai-skills-personal"
SKILLS_WORK="$(cd "${REPO_ROOT}/.." && pwd)/longai-skills-work"

[ -d "${SKILLS_SHARED}" ]   && echo "✅ skills (shared):   ${SKILLS_SHARED}"   || { echo "ℹ️  skills (shared): not found at ${SKILLS_SHARED}"; SKILLS_SHARED=""; }
[ -d "${SKILLS_PERSONAL}" ] && echo "✅ skills (personal): ${SKILLS_PERSONAL}" || { echo "ℹ️  skills (personal): not found at ${SKILLS_PERSONAL}"; SKILLS_PERSONAL=""; }
[ -d "${SKILLS_WORK}" ]     && echo "✅ skills (work):     ${SKILLS_WORK}"     || { echo "ℹ️  skills (work): not found at ${SKILLS_WORK}"; SKILLS_WORK=""; }

DB_PATH="${HOME_DIR}/.longai/state.db"

# ---------------------------------------------------------------------------
# Step 3 — check destination
# ---------------------------------------------------------------------------

mkdir -p "$(dirname "${DEST}")"

if [ -f "${DEST}" ] && [ "${FORCE}" = "0" ]; then
    echo
    echo "❌ ${DEST} already exists. Use --force to overwrite."
    echo "   Tip: back it up first: cp ${DEST} ${DEST}.bak"
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 4 — substitute placeholders + drop comments + drop disabled servers
# ---------------------------------------------------------------------------

"${VENV_PY}" - "${EXAMPLE}" "${DEST}" "${VENV_PY}" "${DB_PATH}" \
    "${SKILLS_SHARED}" "${SKILLS_PERSONAL}" "${SKILLS_WORK}" "${SKIP_PLAYWRIGHT}" <<'PY'
import json
import sys

example_path, dest_path, venv_py, db_path, sk_shared, sk_personal, sk_work, skip_pw = sys.argv[1:9]

with open(example_path) as f:
    cfg = json.load(f)

# Drop any top-level _comment
cfg.pop("_comment", None)

servers = cfg.get("mcpServers", {})
out_servers = {}

for name, spec in servers.items():
    # Drop _comment per server
    spec = {k: v for k, v in spec.items() if k != "_comment"}

    # Skip Playwright if --no-playwright
    if name == "playwright" and skip_pw == "1":
        continue

    # Replace placeholders in command and args
    if spec.get("command") == "PLACEHOLDER_VENV_PYTHON":
        spec["command"] = venv_py

    env = spec.get("env", {})
    new_env = {}
    skill_repo_present = True
    for k, v in env.items():
        if v == "PLACEHOLDER_DB_PATH":
            new_env[k] = db_path
        elif v == "PLACEHOLDER_SKILLS_SHARED":
            if sk_shared: new_env[k] = sk_shared
            else: skill_repo_present = False
        elif v == "PLACEHOLDER_SKILLS_PERSONAL":
            if sk_personal: new_env[k] = sk_personal
            else: skill_repo_present = False
        elif v == "PLACEHOLDER_SKILLS_WORK":
            if sk_work: new_env[k] = sk_work
            else: skill_repo_present = False
        else:
            new_env[k] = v

    # If the server is skill-loader and no skill repos exist, drop it
    if name == "longai-skill-loader" and not new_env:
        continue

    if new_env:
        spec["env"] = new_env

    out_servers[name] = spec

cfg["mcpServers"] = out_servers

with open(dest_path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")

print(f"  wrote {len(out_servers)} server(s) → {dest_path}")
for name in out_servers:
    print(f"    - {name}")
PY

echo
echo "==> ${DEST} ready."
echo "==> Verify with: uv run longai dryrun"
