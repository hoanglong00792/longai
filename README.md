# `longai`

Personal Telegram bot for ≤10 trusted users. Free-models-first agent loop on
OpenRouter (or any OpenAI-compatible endpoint, including local Ollama), strict
budget caps, MCP toolbelt (Playwright + Tavily web search + URL fetch +
EVM + KyberSwap + TA + memory + skills), local CLI for testing without
Telegram.

> **🚨 Setup is non-trivial — read [`SETUP.md`](./SETUP.md) before doing anything.**
>
> It documents must-have prerequisites and the exact bootstrap.
> Skipping a step produces failures that look like bugs but aren't.

> **Status:** v1 working end-to-end. See [`INVARIANTS.md`](./INVARIANTS.md) for
> the 12 non-negotiable rules.

---

## Required vs Optional — at a glance

| What | Status | Why |
|---|---|---|
| `OPENROUTER_API_KEY` (or any OpenAI-compatible key) | **REQUIRED** | The bot panics at boot without it. Get one at https://openrouter.ai. (For Ollama, set this to any non-empty string — Ollama ignores auth.) |
| `~/.longai/config.toml` with `allowed_chat_ids` | **REQUIRED** for `longai bot` | I6: bot silently rejects all messages from non-whitelisted chat_ids. |
| Python ≥ 3.11, `uv`, working `.venv` | **REQUIRED** | Run `bash scripts/setup_venv.sh` once. |
| `~/.longai/mcp.json` | **REQUIRED** | Run `bash scripts/init_mcp.sh` once. Auto-resolves paths. |
| | | |
| `TELEGRAM_BOT_TOKEN` | **OPTIONAL** | Only required for `longai bot`. CLI commands (`chat`, `run`, `test`, `dryrun`, `learn`) work without it. |
| Node.js ≥ 18 with `npx` | **OPTIONAL but RECOMMENDED** | Powers the Playwright MCP (browser automation, charts, login-walled sites). Without it, you lose ~21 `browser_*` tools but keep everything else. |
| `TAVILY_API_KEYS` | **OPTIONAL but RECOMMENDED** | Enables fast web_search MCP (~1-2s per query, 1000 free credits/month per key, comma-separated for rotation). Without it, the agent falls back to Playwright for any web lookup. Get keys at https://tavily.com. |
| `ETHERSCAN_API_KEY` | **OPTIONAL** | Only for the `etherscan_get_abi` tool. Skip if you don't decode contract calldata. |
| `LONGAI_RPC_URL_*` | **OPTIONAL** | Override default LlamaRPC for `eth_call`/`eth_getBalance`/etc. Defaults work fine. |

---

## TL;DR setup (full version: [SETUP.md](./SETUP.md))

```bash
# 0. Prereqs (use only what you need — see table above)
brew install node uv      # node only if you want Playwright; uv always

# 1. Clone + venv
git clone <repo-url> longai && cd longai
bash scripts/setup_venv.sh                       # arch-consistent .venv

# 2. Config + MCP registry
mkdir -p ~/.longai
cp config.example.toml ~/.longai/config.toml     # edit allowed_chat_ids
bash scripts/init_mcp.sh                         # generates ~/.longai/mcp.json

# 3. Env vars (only OPENROUTER_API_KEY is non-negotiable)
export OPENROUTER_API_KEY=sk-or-v1-...
export TELEGRAM_BOT_TOKEN=123:abc...             # only for `longai bot`
export TAVILY_API_KEYS=tvly-...                  # for web_search; skip = degraded but works

# 4. Verify
uv run longai dryrun                             # prints OK + 18-model chain
uv run longai run "what's 2+2"                   # smoke single-shot
bash scripts/live_replay.sh --tier 1             # 2/2 pass = healthy
```

---

## Alternative provider: local Ollama

You can run the entire bot against a **local Ollama server** instead of OpenRouter.
Same code path; just point at a different endpoint.

> **Caveat:** Ollama models — even tool-capable ones like `gemma4:e4b` — handle
> agent loops with multiple tool calls less reliably than the OpenRouter free
> tier. Use Ollama for **casual chat** and simple lookups; switch back to
> OpenRouter for anything multi-step (Playwright research, EVM tracing, etc.).

### Setup

```bash
# 1. Install + run Ollama (https://ollama.com)
brew install ollama
ollama serve &                                   # starts on http://localhost:11434

# 2. Pull a tool-capable small model
ollama pull gemma4:e4b                           # ~4 GB, ~5B effective params

# 3. Point longai at Ollama instead of OpenRouter
export OPENROUTER_BASE_URL=http://localhost:11434/v1
export OPENROUTER_API_KEY=ollama                 # any non-empty string; Ollama ignores auth

# 4. Override the model chain in ~/.longai/config.toml
cat >> ~/.longai/config.toml <<'EOF'

# Ollama mode override — bypass the OpenRouter model auto-refresh
[models_refresh]
policy = "manual"                                # don't try to auto-refresh from OpenRouter API

models = [
    "gemma4:e4b",                                # local default; cheap chat only
]
EOF

# 5. Verify
uv run longai dryrun                             # should show models = [gemma4:e4b]
uv run longai run "say hi"                       # routed to local Ollama
```

When you want to flip back to OpenRouter, just `unset OPENROUTER_BASE_URL` (or set it
back to `https://openrouter.ai/api/v1`) and remove the local override block from
`config.toml`.

### What works on Ollama vs OpenRouter

| Capability | Ollama (`gemma4:e4b`) | OpenRouter free chain |
|---|---|---|
| Casual chat / Q&A | ✅ great | ✅ great |
| Single tool call (calc, recall_memory) | 🟡 hit-or-miss | ✅ reliable |
| Multi-turn tool composition (search → fetch → analyze) | ❌ frequently fails | ✅ reliable |
| Chart / browser automation via Playwright | ❌ context too small | ✅ works |
| Cost | $0 | $0 (free tier) → paid Gemma-4 fallback |
| Latency | ~0.5-3s per call (CPU/GPU bound) | ~1-2s per call (network bound) |
| Privacy | 100% local | OpenRouter logs requests |

**Doyen's recommendation:** keep OpenRouter as primary, use Ollama for
sensitive/private chat or offline development. v1 doesn't support hybrid (one
provider per process). To run both: spawn two `longai chat` processes with
different env vars.

---

## Daily commands

> **macOS users: use the `./longai` wrapper, NOT `uv run longai`.**
> The wrapper forces arm64 and skips `uv run`'s venv re-sync (which silently
> swaps arm64 wheels for x86_64 ones if your shell defaults to x86_64 — e.g.
> via rtk). Without the wrapper you'll get `incompatible architecture`
> ImportErrors after a `uv run`. Add to your shell rc:
>
> ```bash
> alias longai='/Users/<you>/Documents/GitHub/longai/longai'
> ```

```bash
./longai run "<prompt>"      # single-shot, prints JSON envelope
./longai chat                # interactive REPL (no Telegram)
./longai bot                 # Telegram polling
./longai test                # golden prompts vs llmstub (cost = $0)
./longai test --live         # golden prompts vs real OpenRouter (~$0.05)
./longai dryrun              # config validation only, no API calls
./longai refresh             # force-refresh the free models cache
./longai refresh --show      # print current cache without refreshing
./longai learn               # propose memory candidates from history (I7)
./longai learn --apply <p>   # commit reviewed candidates to memory store
```

If you still want `uv run` (e.g., on a non-Mac or x86_64 Linux): `arch -arm64
uv run longai ...` works on Apple Silicon, or just `uv run longai ...` on
non-arm64 systems. The wrapper is mac-arm64 specific protection.

---

## MCP toolbelt (9 servers, ~40 tools)

| Server | Tools | Purpose |
|---|---|---|
| `playwright` (Microsoft, npx) | 21× `browser_*` | Browser automation, charts, login-walled pages |
| `longai-web-search` | `web_search` | Tavily-summarized search (~1-2s, 1000 free credits/mo) |
| `longai-url-fetch` | `fetch_url` | Twitter via vxtwitter, articles via trafilatura |
| `longai-evm` | `eth_call`, `eth_getBalance`, `eth_getCode`, `eth_getStorageAt`, `etherscan_get_abi` | Read-only on-chain queries |
| `longai-on-chain-ta` | `coingecko_token_info`, `dexscreener_pairs`, `combined_token_analysis` | Crypto fundamentals |
| `longai-skill-loader` | `list_skills`, `load_skill` | Read-only access to sibling skill repos (I12) |
| `longai-memory` | `recall_memory`, `save_memory` | Long-term preferences/domain memory (I7) |
| `longai-calc` | `calculate` | Safe arithmetic (no `eval()`) |
| `longai-telegram-out` | `send_message` | Outbound DMs (I8 whitelist gate) |

---

## Invariants

The 12 non-negotiable rules live in [`INVARIANTS.md`](./INVARIANTS.md):

| # | Rule |
|---|---|
| I1 | Don't reinvent the wheel |
| I2 | Free models first |
| I3 | Single chokepoint to OpenRouter (CI grep enforced) |
| I4 | Strict budget caps ($1/day global, $0.25/user, $0.025/call) |
| I5 | Local CLI is non-optional |
| I6 | Personal use only — fork if you want to share |
| I7 | Propose-don't-apply for memory |
| I8 | Outbound chat_id whitelist enforced at tool layer |
| I9 | Token-rename in envelope only |
| I10 | No silent failures |
| I11 | Output sanitizer (wallet/key/seed redaction) |
| I12 | Skills are read-only references |

If you change any of these, the change is wrong.

---

## Tests

```bash
arch -arm64 .venv/bin/python -m pytest -q       # 113 unit tests
bash scripts/check_i3.sh                          # I3 chokepoint enforcement
bash scripts/live_replay.sh --tier 1              # 2 smoke cases against real API
bash scripts/live_replay.sh --reset-cooldowns --throttle 10   # full sweep, 16 cases
```

15 live-replay cases across 5 tiers in `tests/e2e/test_prompts.json` — including
multi-tool composition exercises that probe the agent's exploration ability.

---

## Architecture

See [the design spec](./docs/superpowers/specs/2026-04-27-longai-design.md) for
the full picture (note: `docs/` is gitignored to protect sibling-project privacy;
local copy only). One-line summary: Telegram + CLI both call `Loop.run`, which
calls `BudgetGuard.chat` (the only chat-completion caller), which iterates a
fallback chain of free models with strict per-call/per-user/global caps.
