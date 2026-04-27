#!/usr/bin/env bash
#
# scripts/live_replay.sh — run a batch of prompts through `longai run`
# using a REAL OpenRouter key. Outputs per-case trace dirs + a summary TSV.
#
# Usage:
#   export OPENROUTER_API_KEY=sk-or-v1-...
#   ./scripts/live_replay.sh                              # all tiers
#   ./scripts/live_replay.sh --tier 1                     # tier-1 smoke only
#   ./scripts/live_replay.sh --prompts tests/e2e/foo.json --tier 2
#   ./scripts/live_replay.sh --max-cases 3                # first 3 cases only
#   ./scripts/live_replay.sh --throttle 10                # sleep 10s between cases
#                                                          (recommended for full sweeps —
#                                                           OpenRouter free tier rate-limits
#                                                           around 20 req/min per model)
#   ./scripts/live_replay.sh --reset-cooldowns            # clear cooldowns table first
#                                                          (useful after a previous failed
#                                                           run benched all models)
#   ./scripts/live_replay.sh --ids calc_tool,evm_balance  # run only these case ids
#                                                          (combines with --tier)
#
# Output:
#   test-results/live_replay_<UTC-TS>/
#     ├── 00_meta.txt                — run metadata (git rev, prompts file, tier, env summary)
#     ├── 01_summary.tsv             — id, tier, status, spend_usd, model, stopped, trace_dir, fails
#     ├── 02_envelopes.jsonl         — one envelope per case (full JSON, post-sanitize)
#     └── cases/<id>/                — per-case trace dir from --trace-dir
#
# Exits non-zero if any case FAILS its assertions.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# Force native arch on Apple Silicon — venv's universal Python may pick x86_64
# when invoked from rtk/shell wrappers, which then fails to load arm64 wheels
# (universal-binary arch ambiguity). Bypass `uv run` and call the venv's Python
# directly with `arch -arm64` — both are needed:
#   - `uv run` may re-fork without inheriting arch
#   - .venv/bin/python is a universal binary that must be told which slice
# Detect Apple Silicon at hardware level — `uname -m` lies inside x86_64 subshells
# (e.g., rtk wrappers default to x86_64 mode). `sysctl -n hw.optional.arm64` returns
# "1" on Apple Silicon regardless of the current process arch.
ARCH_PREFIX=""
if [ "$(uname)" = "Darwin" ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
    ARCH_PREFIX="arch -arm64"
fi

# Drop a polluting VIRTUAL_ENV (e.g., a stale myenv from earlier sessions).
unset VIRTUAL_ENV

# Resolve Python binary
if [ ! -x ".venv/bin/python" ]; then
    echo "ERROR: .venv/bin/python not found. Run scripts/setup_venv.sh first." >&2
    exit 2
fi
PYBIN="${REPO_ROOT}/.venv/bin/python"

PROMPTS="tests/e2e/test_prompts.json"
TIER=0
MAX_CASES=0       # 0 = unlimited
THROTTLE_S=0      # seconds to sleep between cases (recommended 5+ for full sweeps)
RESET_COOLDOWNS=0 # if 1, clear the cooldowns table before running
IDS=""            # comma-separated case ids to run (empty = all matching cases)

while [ $# -gt 0 ]; do
  case "$1" in
    --prompts) PROMPTS="$2"; shift 2 ;;
    --tier) TIER="$2"; shift 2 ;;
    --max-cases) MAX_CASES="$2"; shift 2 ;;
    --throttle) THROTTLE_S="$2"; shift 2 ;;
    --reset-cooldowns) RESET_COOLDOWNS=1; shift ;;
    --ids) IDS="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -z "${OPENROUTER_API_KEY_FILE:-}" ]; then
  echo "ERROR: missing OpenRouter key. Set OPENROUTER_API_KEY or OPENROUTER_API_KEY_FILE." >&2
  exit 2
fi

if [ ! -f "${PROMPTS}" ]; then
  echo "ERROR: prompts file not found: ${PROMPTS}" >&2
  exit 2
fi

TS="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
OUT="${REPO_ROOT}/test-results/live_replay_${TS}"
mkdir -p "${OUT}/cases"

{
  echo "longai live_replay: ${TS} UTC"
  echo "repo:       ${REPO_ROOT}"
  echo "git rev:    $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo "prompts:    ${PROMPTS}"
  echo "tier:       ${TIER}"
  echo "max_cases:  ${MAX_CASES}"
  echo
  echo "env:"
  echo "  OPENROUTER_API_KEY=<set: $([ -n "${OPENROUTER_API_KEY:-}" ] && echo yes || echo no)>"
  echo "  OPENROUTER_API_KEY_FILE=<set: $([ -n "${OPENROUTER_API_KEY_FILE:-}" ] && echo yes || echo no)>"
  echo "  TELEGRAM_BOT_TOKEN=<set: $([ -n "${TELEGRAM_BOT_TOKEN:-}" ] && echo yes || echo no)>"
} > "${OUT}/00_meta.txt"

echo -e "id\ttier\tstatus\tspend_usd\tmodel\tstopped\tturns\ttrace_dir\tassertion_failures" \
  > "${OUT}/01_summary.tsv"
: > "${OUT}/02_envelopes.jsonl"

# Optional: reset cooldowns so prior run's bench-pressed models recover immediately
if [ "${RESET_COOLDOWNS}" = "1" ]; then
    DB_PATH="${HOME}/.longai/state.db"
    if [ -f "${DB_PATH}" ]; then
        echo "==> Clearing cooldowns from ${DB_PATH}"
        $ARCH_PREFIX "${PYBIN}" -c "
import sqlite3
c = sqlite3.connect('${DB_PATH}')
deleted = c.execute('DELETE FROM cooldowns').rowcount
c.commit()
print(f'  cleared {deleted} cooldown row(s)')
"
    fi
fi

# Emit per-case TSV: id<TAB>tier<TAB>json(message)<TAB>json(expect)
python3 - <<'PY' "${PROMPTS}" "${TIER}" "${MAX_CASES}" "${IDS}" > "${OUT}/_cases.tsv"
import json, sys
path, tier, max_cases, ids_csv = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
id_filter = {s.strip() for s in ids_csv.split(",") if s.strip()} if ids_csv else None
with open(path) as f:
    cases = json.load(f)
out = []
for c in cases:
    if tier and int(c.get("tier", 0)) != tier:
        continue
    cid = c.get("id"); msg = c.get("message")
    if not cid or not msg:
        continue
    if id_filter is not None and cid not in id_filter:
        continue
    out.append(c)
if id_filter:
    seen = {c["id"] for c in out}
    missing = id_filter - seen
    if missing:
        print(f"ERROR: --ids contains unknown case(s): {sorted(missing)}", file=sys.stderr)
        raise SystemExit(2)
if max_cases > 0:
    out = out[:max_cases]
for c in out:
    print(f"{c['id']}\t{c.get('tier',0)}\t"
          f"{json.dumps(c['message'], separators=(',',':'))}\t"
          f"{json.dumps(c.get('expect',{}), separators=(',',':'))}")
PY

FAILURES=0
TOTAL=0
while IFS=$'\t' read -r ID CASE_TIER MESSAGE_JSON EXPECT_JSON; do
  TOTAL=$((TOTAL+1))
  echo
  echo "[live_replay] ${ID} (tier=${CASE_TIER})"

  # Decode message JSON to a shell string
  MESSAGE="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]))' "${MESSAGE_JSON}")"

  CASE_TRACE="${OUT}/cases/${ID}"
  mkdir -p "${CASE_TRACE}"

  # Tier-6/7 cases chain multiple tools — bump max_turns above the global default
  # of 5 so weaker free models have headroom to finish the composition.
  MAX_TURNS_ARGS=()
  if [ "${CASE_TIER}" -ge 6 ]; then
      MAX_TURNS_ARGS=(--max-turns 10)
  fi

  # Run the case. Capture stdout (envelope JSON) and exit code.
  set +e
  ENVELOPE="$($ARCH_PREFIX "${PYBIN}" -m longai run --json --user-id -1 --trace-dir "${CASE_TRACE}" "${MAX_TURNS_ARGS[@]}" -- "${MESSAGE}" 2>"${CASE_TRACE}/stderr.txt")"
  RC=$?
  set -e

  echo "${ENVELOPE}" >> "${OUT}/02_envelopes.jsonl"

  # Assert against expect block
  ASSERTION="$(python3 - <<'PY' "${EXPECT_JSON}" "${ENVELOPE}" "${RC}"
import json, sys

expect = json.loads(sys.argv[1] or "{}")
try:
    env = json.loads(sys.argv[2])
except Exception:
    print("FAIL\tenvelope was not valid JSON")
    raise SystemExit(0)
rc = int(sys.argv[3])
fails = []

# stopped (single match)
if "stopped" in expect and env.get("stopped") != expect["stopped"]:
    fails.append(f"stopped={env.get('stopped')!r} != expected {expect['stopped']!r}")

# stopped_in (any-of)
if "stopped_in" in expect and env.get("stopped") not in expect["stopped_in"]:
    fails.append(f"stopped={env.get('stopped')!r} not in {expect['stopped_in']!r}")

# reply_contains (all-of)
result = env.get("result", "") or ""
for needle in expect.get("reply_contains") or []:
    if needle not in result:
        fails.append(f"reply missing {needle!r}")

# reply_contains_any (any-of)
any_match = expect.get("reply_contains_any") or []
if any_match and not any(needle in result for needle in any_match):
    fails.append(f"reply has none of {any_match!r}")

# reply_not_contains (none-of)
for needle in expect.get("reply_not_contains") or []:
    if needle in result:
        fails.append(f"reply contains forbidden {needle!r}")

# spend_max
if "spend_max" in expect:
    spend = float(env.get("spend_usd") or 0)
    if spend > expect["spend_max"]:
        fails.append(f"spend ${spend:.4f} > cap ${expect['spend_max']:.4f}")

# min_turns — exploration cases must actually call multiple tools
if "min_turns" in expect:
    turns = int(env.get("turns") or 0)
    if turns < expect["min_turns"]:
        fails.append(f"turns={turns} < min {expect['min_turns']} (agent skipped tool composition)")

# tier — exact match
if "tier" in expect and env.get("tier") != expect["tier"]:
    fails.append(f"tier={env.get('tier')!r} != expected {expect['tier']!r}")

# model_in — model must be in the listed allowlist
if "model_in" in expect:
    allow = expect["model_in"]
    if env.get("model") not in allow:
        fails.append(f"model={env.get('model')!r} not in {allow!r}")

# rc check (don't fail just on rc — `stopped != final` already implies non-zero)
status = "PASS" if not fails else "FAIL"
print(status + "\t" + "; ".join(fails))
PY
)"
  STATUS="${ASSERTION%%$'\t'*}"
  ASSERTION_FAILS="${ASSERTION#*$'\t'}"
  [ "${STATUS}" = "PASS" ] || FAILURES=$((FAILURES + 1))

  # Extract a few summary fields from the envelope for the TSV
  SPEND="$(python3 -c 'import json,sys; d=json.loads(sys.argv[1] or "{}"); print(d.get("spend_usd",0))' "${ENVELOPE}" 2>/dev/null || echo 0)"
  MODEL="$(python3 -c 'import json,sys; d=json.loads(sys.argv[1] or "{}"); print(d.get("model",""))' "${ENVELOPE}" 2>/dev/null || echo "")"
  STOPPED="$(python3 -c 'import json,sys; d=json.loads(sys.argv[1] or "{}"); print(d.get("stopped",""))' "${ENVELOPE}" 2>/dev/null || echo "")"
  TURNS="$(python3 -c 'import json,sys; d=json.loads(sys.argv[1] or "{}"); print(d.get("turns",0))' "${ENVELOPE}" 2>/dev/null || echo 0)"

  echo -e "${ID}\t${CASE_TIER}\t${STATUS}\t${SPEND}\t${MODEL}\t${STOPPED}\t${TURNS}\t${CASE_TRACE}\t${ASSERTION_FAILS}" \
    >> "${OUT}/01_summary.tsv"

  printf "  status=%s spend=$%s model=%s stopped=%s turns=%s\n" \
    "${STATUS}" "${SPEND}" "${MODEL}" "${STOPPED}" "${TURNS}"
  [ "${STATUS}" = "PASS" ] || echo "  fails: ${ASSERTION_FAILS}"

  # Throttle between cases to give OpenRouter free tier time to recover from
  # rate-limit cooldowns. Recommended 5+ for full sweeps.
  if [ "${THROTTLE_S}" -gt 0 ] && [ "${TOTAL}" -lt 999 ]; then
      sleep "${THROTTLE_S}"
  fi
done < "${OUT}/_cases.tsv"

rm -f "${OUT}/_cases.tsv"

PASS=$((TOTAL - FAILURES))
echo
echo "==> live_replay written to: ${OUT}"
echo "==> ${PASS}/${TOTAL} pass, ${FAILURES} fail"
echo "==> see ${OUT}/01_summary.tsv"

if [ "${FAILURES}" -gt 0 ]; then
  exit 1
fi
