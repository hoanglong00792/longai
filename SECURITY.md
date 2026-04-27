# Security Policy

`longai` runs an LLM agent loop with access to API keys, wallet addresses, and
read-only on-chain queries. Output is sanitized before reaching the user (I11),
budget caps protect against runaway spend (I4), and outbound Telegram messages
are gated by a chat ID whitelist (I8). If you find a way to bypass any of
these, I want to know.

## Reporting a vulnerability

**Do not open a public issue for security reports.**

Instead, email **hoanglong00792@gmail.com** with:

- A short description of the issue
- Steps to reproduce (or a proof-of-concept)
- The affected file(s) and approximate line numbers
- Your assessment of impact (e.g. "leaks API key in error path", "bypasses budget cap")

I'll acknowledge within a few days. If the issue is confirmed, I'll work on a
fix and credit you in the commit unless you prefer to stay anonymous.

## In scope

The following are considered security-relevant:

- The output sanitizer in `src/longai/security.py` (wallet/key/seed redaction)
- Budget enforcement in `src/longai/budget_guard.py` (per-call/per-user/global caps)
- The outbound Telegram whitelist in `src/longai_mcps/telegram_out/`
- The MCP allowlist in `src/longai/cli.py` (`_build_stack`)
- API key handling in `src/longai/config.py` (env vars, file-based secrets)
- Any path that could leak `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`,
  `TAVILY_API_KEYS`, `ETHERSCAN_API_KEY`, or `LONGAI_RPC_URL_*` into logs,
  tracebacks, traces, or model context

## Out of scope

- Issues in upstream dependencies (report to those projects directly)
- Issues that require an attacker to already have your API keys
- Misconfiguration of personal `~/.longai/config.toml` (e.g. permissive `allowed_chat_ids`)
- Anything requiring physical access to the host running the bot

## Threat model

This is a personal bot for ≤10 trusted users (I6). It is **not** designed to
be a multi-tenant service or to defend against malicious users on shared
infrastructure. If you intend to run it in a less trusted environment, audit
the codebase yourself first.
