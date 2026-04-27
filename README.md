# `longai`

Personal Telegram bot for ≤10 trusted users. Free-models-first agent loop on
OpenRouter, strict budget caps, MCP toolbelt, local CLI for testing without
Telegram.

> **Status:** v1 in development. See [`INVARIANTS.md`](./INVARIANTS.md) for the
> non-negotiable rules and [`docs/superpowers/specs/2026-04-27-longai-design.md`](./docs/superpowers/specs/2026-04-27-longai-design.md)
> for the design spec.

## Quick start

```bash
# 1. Install
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# 2. Configure
mkdir -p ~/.longai
cp config.example.toml ~/.longai/config.toml
cp mcp.example.json ~/.longai/mcp.json   # ships with Playwright MCP enabled
# Edit ~/.longai/config.toml — set allowed_chat_ids
# Set env: OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN
# Playwright MCP requires Node.js + npx (one-time): https://nodejs.org

# 3. Test it locally without Telegram
longai dryrun                       # validate config + spawn MCPs
longai run "say hello"              # single-shot
longai chat                         # REPL
longai test                         # golden prompts vs llmstub
longai test --live                  # smoke against real OR free chain

# 4. Run the bot
longai bot
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
