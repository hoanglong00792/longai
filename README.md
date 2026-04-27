# `longai`

Personal Telegram bot for ≤10 trusted users. Free-models-first agent loop on
OpenRouter, strict budget caps, MCP toolbelt (Playwright + EVM + KyberSwap +
TA + memory + skills), local CLI for testing without Telegram.

> **🚨 Setup is non-trivial — read [`SETUP.md`](./SETUP.md) before doing anything.**
>
> It documents the **must-have** prerequisites (Node.js + npx for Playwright,
> arch-consistent venv on Apple Silicon, OpenRouter key, etc.) and the exact
> 6-step bootstrap. Skipping a step will produce failures that look like bugs
> but aren't.

> **Status:** v1 working end-to-end. See [`INVARIANTS.md`](./INVARIANTS.md) for the
> 12 non-negotiable rules and [`docs/superpowers/specs/2026-04-27-longai-design.md`](./docs/superpowers/specs/2026-04-27-longai-design.md)
> for the design spec.

## TL;DR setup (full version: [SETUP.md](./SETUP.md))

```bash
# 0. Prereqs: Python ≥3.11, uv, Node.js ≥18 with npx
brew install node uv  # if missing

# 1. Clone + venv
git clone <repo-url> longai && cd longai
bash scripts/setup_venv.sh                       # arch-consistent .venv

# 2. Config + MCP registry
mkdir -p ~/.longai
cp config.example.toml ~/.longai/config.toml     # edit allowed_chat_ids
bash scripts/init_mcp.sh                         # generates ~/.longai/mcp.json
                                                  # auto-resolves abs paths

# 3. Env vars
export OPENROUTER_API_KEY=sk-or-v1-...           # required (panic at boot if missing)
export TELEGRAM_BOT_TOKEN=123:abc...             # required for `longai bot`

# 4. Verify
uv run longai dryrun                             # config OK
uv run longai run "what's 2+2"                   # smoke single-shot
bash scripts/live_replay.sh --tier 1             # 2/2 pass = healthy
```

## Daily commands

```bash
longai run "<prompt>"        # single-shot, prints JSON envelope
longai chat                  # interactive REPL (no Telegram)
longai bot                   # Telegram polling
longai test                  # golden prompts vs llmstub (cost = $0)
longai test --live           # golden prompts vs real OpenRouter
longai dryrun                # config validation only, no API calls
longai learn                 # propose memory candidates from history (I7)
longai learn --apply <path>  # commit reviewed candidates to memory store
```

## Invariants

The 12 non-negotiable rules live in [`INVARIANTS.md`](./INVARIANTS.md):
- I1 don't reinvent the wheel
- I2 free models first
- I3 single chokepoint to OpenRouter (CI grep enforced)
- I4 strict budget caps
- I5 local CLI is non-optional
- I6 personal use only — fork if you want to share
- I7 propose-don't-apply for memory
- I8 outbound chat_id whitelist
- I9 token-rename in envelope only
- I10 no silent failures
- I11 output sanitizer (wallet/key/seed redaction)
- I12 skills are read-only references

If you change any of these, the change is wrong.

## Architecture

See [the design spec](./docs/superpowers/specs/2026-04-27-longai-design.md) for
the full picture. One-line summary: Telegram + CLI both call `Loop.run`, which
calls `BudgetGuard.chat` (the only OpenRouter caller), which calls a fallback
chain of free Gemma-4 models with strict per-call/per-user/global caps.
