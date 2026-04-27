#!/usr/bin/env bash
# scripts/install_skills.example.sh — OPTIONAL example installer.
#
# OPTIONAL. The bot does NOT need this to run. It's an example pattern for
# curating which skills your longai-skill-loader MCP exposes when you keep a
# large skill repo but only want a subset visible to the bot.
#
# What it does:
#   Copies a hand-picked subset of skills from a source repo into
#   ~/.longai/skills-shared/skills/, so I12's read-only skill_loader sees
#   only the curated set instead of the full library.
#
# Why you might want this:
#   - Your skill repo includes dev/codebase tooling that doesn't belong in a
#     Telegram bot's context (e.g. git helpers, code reviewers, test gens).
#   - You want a tighter, lower-token context for free-models-first runs.
#   - Different harnesses (Claude Code vs longai) consuming the same skill
#     repo with different subsets.
#
# Why you might NOT want this:
#   - You're fine with the bot seeing every SKILL.md you have. Just point
#     [skills].shared at the source repo and skip this script entirely.
#
# Customizing:
#   Copy this file (e.g. to install_skills.sh) and edit the ALLOWLIST array
#   below to match your harness. The list shipped here reflects one author's
#   choices for an autonomous bot — the right set for YOUR bot depends on
#   what tools and personas you want it carrying around. There is no
#   canonical answer; this is an example.
#
# Counterpart:
#   ../longai-skills-shared/install.sh installs the same source repo into
#   Claude Code's ~/.claude/ instead — different harness, different layout.
#
# Usage:
#   bash scripts/install_skills.example.sh                    # default source: ../longai-skills-shared
#   bash scripts/install_skills.example.sh --source <path>    # override source repo
#   bash scripts/install_skills.example.sh --dest <path>      # override destination root
#   bash scripts/install_skills.example.sh --list             # print allowlist and exit
#   bash scripts/install_skills.example.sh --dry-run          # show actions, change nothing
#
set -euo pipefail

# EXAMPLE allowlist — edit to match your harness.
#
# These four were the author's pick for an autonomous Telegram bot:
#   - kyberswap-shared : reference data (token registry, API spec)
#   - quote            : KyberSwap quote tool (read-only)
#   - on-chain-ta      : token analysis (matches longai-on-chain-ta MCP)
#   - debank-balance   : portfolio fetch via Playwright
#
# Your set will differ. Add/remove freely; each name must match a directory
# under <source>/skills/<name>/SKILL.md.
ALLOWLIST=(
  "kyberswap-shared"
  "quote"
  "on-chain-ta"
  "debank-balance"
)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HOME_DIR="$(cd "${HOME}" && pwd)"

SOURCE="${REPO_ROOT}/../longai-skills-shared"
DEST="${HOME_DIR}/.longai/skills-shared"
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --dest)   DEST="$2";   shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --list)
      printf '%s\n' "${ALLOWLIST[@]}"
      exit 0
      ;;
    -h|--help)
      sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Resolve to an absolute path if it exists (cd ... pwd); otherwise leave as-is
# so the existence check below produces a clear error.
if [ -d "${SOURCE}" ]; then
    SOURCE="$(cd "${SOURCE}" && pwd)"
fi
SRC_SKILLS="${SOURCE}/skills"

echo "==> longai skill installer"
echo "    source: ${SOURCE}"
echo "    dest:   ${DEST}"
echo "    skills: ${ALLOWLIST[*]}"
echo

if [ ! -d "${SRC_SKILLS}" ]; then
    echo "❌ source skills dir not found: ${SRC_SKILLS}" >&2
    echo "   Pass --source <path> to point at your longai-skills-shared checkout." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Pre-flight: every allowlisted skill must exist in source.
# ---------------------------------------------------------------------------
MISSING=0
for name in "${ALLOWLIST[@]}"; do
    if [ ! -f "${SRC_SKILLS}/${name}/SKILL.md" ]; then
        echo "❌ missing in source: ${name}/SKILL.md" >&2
        MISSING=$((MISSING + 1))
    fi
done
if [ "${MISSING}" -gt 0 ]; then
    echo "==> ${MISSING} skill(s) missing — aborting." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Copy: prune stale + replace each allowlisted skill.
# ---------------------------------------------------------------------------
DEST_SKILLS="${DEST}/skills"

if [ "${DRY_RUN}" = "1" ]; then
    echo "[dry-run] would mkdir -p ${DEST_SKILLS}"
    if [ -d "${DEST_SKILLS}" ]; then
        for existing in "${DEST_SKILLS}"/*/; do
            [ -d "${existing}" ] || continue
            n="$(basename "${existing}")"
            keep=0
            for a in "${ALLOWLIST[@]}"; do [ "${n}" = "${a}" ] && keep=1; done
            [ "${keep}" = "0" ] && echo "[dry-run] would prune: ${n}"
        done
    fi
    for name in "${ALLOWLIST[@]}"; do
        echo "[dry-run] would install: ${name}"
    done
else
    mkdir -p "${DEST_SKILLS}"

    # Prune anything in dest that's no longer in the allowlist. Only touches
    # subdirectories of DEST_SKILLS — never the parent.
    if [ -d "${DEST_SKILLS}" ]; then
        for existing in "${DEST_SKILLS}"/*/; do
            [ -d "${existing}" ] || continue
            n="$(basename "${existing}")"
            keep=0
            for a in "${ALLOWLIST[@]}"; do [ "${n}" = "${a}" ] && keep=1; done
            if [ "${keep}" = "0" ]; then
                rm -rf "${existing}"
                echo "  pruned:    ${n} (not in allowlist)"
            fi
        done
    fi

    for name in "${ALLOWLIST[@]}"; do
        rm -rf "${DEST_SKILLS:?}/${name}"
        cp -R "${SRC_SKILLS}/${name}" "${DEST_SKILLS}/${name}"
        echo "  installed: ${name}"
    done
fi

# ---------------------------------------------------------------------------
# Wire DEST into ~/.longai/config.toml [skills].shared.
# Only modifies the empty default; never overwrites a different value.
# ---------------------------------------------------------------------------
CONFIG_TOML="${HOME_DIR}/.longai/config.toml"
echo
if [ ! -f "${CONFIG_TOML}" ]; then
    echo "ℹ️  ${CONFIG_TOML} not found — skipping config update."
    echo "   Once you create it (cp config.example.toml ${CONFIG_TOML}), set:"
    echo "     [skills]"
    echo "     shared = \"${DEST}\""
elif [ "${DRY_RUN}" = "1" ]; then
    echo "[dry-run] would set [skills].shared = \"${DEST}\" in ${CONFIG_TOML} (only if empty)"
else
    VENV_PY="${REPO_ROOT}/.venv/bin/python"
    [ -x "${VENV_PY}" ] || VENV_PY="$(command -v python3 || command -v python)"
    "${VENV_PY}" - "${CONFIG_TOML}" "${DEST}" <<'PY'
import os, re, sys, tomllib

cfg_path, dest = sys.argv[1], sys.argv[2]

with open(cfg_path, "rb") as f:
    cfg = tomllib.load(f)
current = cfg.get("skills", {}).get("shared", "")
current_expanded = os.path.expanduser(current) if current else ""

if current_expanded == dest:
    print(f"ℹ️  [skills].shared already points to {dest} — no change.")
    sys.exit(0)
if current:
    print(f"⚠️  [skills].shared already set to: {current}")
    print(f"    leaving it alone. To use the new path, edit manually:")
    print(f"      shared = \"{dest}\"")
    sys.exit(0)

# current is empty — rewrite the empty `shared = ""` line, but ONLY inside
# the [skills] table, not any other section that might also use that key.
text = open(cfg_path).read()
m = re.search(r'(^\[skills\][^\n]*\n)(.*?)(?=^\[|\Z)', text,
              re.MULTILINE | re.DOTALL)
if not m:
    print("⚠️  no [skills] section found — please add manually:")
    print(f"      [skills]")
    print(f"      shared = \"{dest}\"")
    sys.exit(0)

header, body = m.group(1), m.group(2)
new_body, n = re.subn(
    r'(^shared\s*=\s*)""',
    lambda mm: mm.group(1) + f'"{dest}"',
    body,
    count=1,
    flags=re.MULTILINE,
)
if n == 0:
    print('⚠️  found [skills] but no empty `shared = ""` line — leaving alone.')
    print(f'    To use the new path, set: shared = "{dest}"')
    sys.exit(0)

new_text = text[: m.start(2)] + new_body + text[m.end(2) :]
with open(cfg_path, "w") as f:
    f.write(new_text)
print(f'✅ wrote [skills].shared = "{dest}" → {cfg_path}')
PY
fi

echo
echo "==> Done."
echo "==> Next: regenerate MCP config so the bot picks up the new path:"
echo "      bash scripts/init_mcp.sh --force"
