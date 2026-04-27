# Setup — must-have steps

> **Read this file before running anything.** Skipping a step here means
> failures later that look like code bugs but aren't.

---

## Prerequisites (system-level)

These are **required** for the default MCP toolbelt. Install once per machine.

| Requirement | Why | Install |
|---|---|---|
| **Python ≥ 3.11** | `tomllib` stdlib + project base | https://www.python.org/downloads/ |
| **`uv`** | Project dependency manager | `pip install uv` or `brew install uv` |
| **Node.js ≥ 18 with `npx`** | **REQUIRED** — Playwright MCP runs via `npx -y @playwright/mcp@latest`. Without this, the bot has no browser tool. | `brew install node` or https://nodejs.org |

> ⚠ **Playwright MCP is not optional.** It is the bot's only browser/web-research tool.
> Every default MCP toolbelt ships with it enabled. If you skip Node, the bot can
> still chat but can't navigate Etherscan, DexScreener, Twitter, or any web page.

---

## One-time setup

Run these in order. **Do not skip steps.**

### 1. Clone

```bash
cd ~/Documents/GitHub
git clone <repo-url> longai
cd longai
```

### 2. Create the virtual environment

```bash
bash scripts/setup_venv.sh
```

This creates an arch-correct `.venv` (Apple Silicon: arm64 forced via `arch -arm64`)
and installs the project + dev extras. It also runs the test suite as a smoke check.

> **Why a script and not just `uv venv`?** On Apple Silicon, the default Python is
> a universal binary (x86_64 + arm64). When invoked under x86_64 mode (rtk wrappers,
> some shell configs), the venv loads x86_64 and then can't load arm64-only wheels
> like `pydantic_core`. This script forces arm64 consistently. See `scripts/setup_venv.sh`
> for the full story.

### 3. Configure

```bash
mkdir -p ~/.longai
cp config.example.toml ~/.longai/config.toml
$EDITOR ~/.longai/config.toml
```

Edit at minimum:
- `allowed_chat_ids` — your Telegram chat_id (DM @userinfobot to find it)
- `allowed_outbound_chat_ids` — chat_ids the agent can DM via the `telegram_out` tool

The other defaults (model chain, budget caps, paths) work as-is.

### 4. Generate `~/.longai/mcp.json` — the MCP server registry

```bash
bash scripts/init_mcp.sh
```

This:
1. Verifies `npx` is installed (Playwright requirement)
2. Verifies `.venv/bin/python` exists
3. Auto-detects sibling skill repos (`longai-skills-{shared,personal,work}`)
4. Writes `~/.longai/mcp.json` with absolute paths for THIS machine

If any prerequisite fails, the script aborts with a clear message.

### 5. Set environment variables

```bash
# Required
export OPENROUTER_API_KEY=sk-or-v1-...     # get at https://openrouter.ai
export TELEGRAM_BOT_TOKEN=123:abc...        # required for `longai bot`; optional for chat/run/test

# Recommended — enables web_search MCP (Tavily, 1000 free credits/month per key)
export TAVILY_API_KEYS=tvly-...,tvly-...    # comma-separated for rotation; get at https://tavily.com
# (legacy single-key form also accepted: TAVILY_API_KEY=tvly-...)

# Optional — enables EVM RPC tools beyond default LlamaRPC
export ETHERSCAN_API_KEY=...                # for the etherscan_get_abi tool
```

For persistence, add these to `~/.zshrc` or use a `.env` file (see `.env.example`).

> **Why TAVILY_API_KEYS is recommended:** the bot defaults to Playwright for any
> web research, which is slow (~10-30s per query). Tavily returns a synthesized
> answer plus 3 sources in ~1-2s and is token-cheap. With Tavily configured, the
> agent learns to use `web_search` for general "what is X / latest Y" questions
> and only escalates to Playwright for charts, login-walled pages, and
> interactive sites.

### 6. Verify everything

```bash
uv run longai dryrun
```

Expected: prints `config OK` and lists models, caps, paths.

```bash
uv run longai run "what's 2+2"
```

Expected: a JSON envelope with `stopped: "final"` and `spend_usd` near 0 (free tier).

```bash
bash scripts/live_replay.sh --tier 1
```

Expected: 2/2 PASS on smoke prompts.

---

## Daily use

```bash
# Activate venv (only needed for raw `python` invocations)
source .venv/bin/activate

# Single-shot
longai run "use playwright to summarize https://example.com"

# Interactive REPL
longai chat

# Run the Telegram bot
longai bot

# Memory daemon (propose-don't-apply per I7)
longai learn
longai learn --apply ~/.longai/candidates-*.md
```

---

## Re-init after `git pull`

When pulling new changes, especially if dependencies or MCP servers change:

```bash
bash scripts/setup_venv.sh --keep-existing   # update deps without wiping venv
bash scripts/init_mcp.sh --force             # refresh ~/.longai/mcp.json
```

---

## Troubleshooting

### "incompatible architecture (have 'arm64', need 'x86_64')"
Your `.venv/bin/python` is loading under x86_64 but the wheels are arm64.
Run `bash scripts/setup_venv.sh` (no flags — it wipes and recreates).

### "playwright is not enabled" / model says "I have no browser tool"
Your `~/.longai/mcp.json` doesn't have Playwright. Run `bash scripts/init_mcp.sh --force`.
Or check that `npx` is on PATH: `which npx`.

### "MCP config not found at ~/.longai/mcp.json"
Run `bash scripts/init_mcp.sh`.

### "OPENROUTER_API_KEY not set — exiting"
You haven't set the env var. See step 5 above. The bot panics at boot if missing —
this is intentional (per I3, no key = no bot).

### "Daily budget reached"
You've hit the I4 cap of $1.00/day global or $0.25/user/day. Resets at UTC midnight.
This is intentional and non-bypassable from the CLI. If you need a higher cap for
a specific project, edit `~/.longai/config.toml` `[caps]` (the discipline limit, not
the I4 invariant).

### "Event loop is closed" warning at end of `longai run`
Cosmetic noise from MCP subprocess teardown on exit. Doesn't affect the result.
Filed for cleanup but does not require action.

### Tests fail when invoked through `rtk` or other shell wrappers
Some wrappers force x86_64. Use `arch -arm64 .venv/bin/python -m pytest -v` instead.

---

## What "must-have" means concretely

Setup is non-optional for these — you don't get a working bot without them:

1. **Node.js + `npx`** — Playwright MCP doesn't run without it
2. **`scripts/setup_venv.sh`** — arch-consistent venv (Mac users especially)
3. **`scripts/init_mcp.sh`** — bootstrap MCP registry with absolute paths
4. **`OPENROUTER_API_KEY`** — boot-panic on missing per I3
5. **`~/.longai/config.toml` with `allowed_chat_ids`** — bot rejects every Telegram message otherwise (per I6)

If you can't satisfy all 5, the bot will fail loudly, not silently.
