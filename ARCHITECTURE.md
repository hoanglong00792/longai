# Architecture

Telegram and CLI both call `Loop.run`, which calls `BudgetGuard.chat` (the only
chat-completion caller, per I3), which iterates a tier-aware fallback chain of
free OpenRouter models with strict per-call/per-user/global caps (I4).

Twelve non-negotiable rules: see [`INVARIANTS.md`](./INVARIANTS.md).

---

## MCP toolbelt — 9 servers, ~40 tools

The agent reaches the world through these MCP servers, configured by
`scripts/init_mcp.sh` and registered in `~/.longai/mcp.json`.

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

## Tiered model routing

`BudgetGuard.chat(tier=...)` selects which model chain to iterate. The loop
classifies each user message via regex into S / M / L (or honors `/quick` and
`/deep` prefixes) and reads tier-specific `max_turns` and `wall_clock_s` from
caps.

Default tiers, when configured via `[models.tier_S/M/L]` in `config.toml`:

| Tier | Use case | Typical free models | Default turns | Default wall-clock |
|---|---|---|---|---|
| S | Greetings, single tool call, simple math | 9-30B | 3 | 15s |
| M | Default; factual asks, light tool use | 26-70B | 5 | 30s |
| L | Multi-source research, comparisons, synthesis | 120B+ | 12 | 90s |
| fallback | Hit only when every tier chain is cooled | paid Gemma-4 floor | (per call) | (per call) |

Legacy single-chain configs (`models = [...]`) continue to work — they map all
tiers to the same chain. See `config.example.toml` for the tiered schema.

### Skill complexity & mid-loop tier escalation

`SKILL.md` frontmatter accepts an optional `complexity: S | M | L` field
(defaults to `M`). When the agent calls `load_skill` mid-conversation, the
skill_loader MCP returns the complexity alongside the body, and the loop
escalates tier upward (never downward) for the remaining turns. A skill
declared `complexity: L` loaded on turn 2 of an M-tier run upgrades the rest
of the run to L's chain and turn budget.

```yaml
---
name: deep-research
description: Multi-source synthesis, comparison, deep analysis.
complexity: L
---
```

Routing: classifier sets initial tier from the user message → load_skill may
bump it up → `BudgetGuard.chat(tier=...)` picks the chain accordingly.

---

## Persistence

SQLite under `~/.longai/state.db` holds messages (sliding window for context),
debits (daily spend ledger for budget enforcement), cooldowns (per-model bench
periods after retryable failures), memories (with `propose-don't-apply` per
I7), and traces (one row per `Loop.run`). WAL mode, no ORM.

### Migrations

Hand-written `ALTER TABLE` snippets. There is no migrations framework — the
schema stays flat until at least three migrations exist (per the rationale in
`INVARIANTS.md` § "On adding new invariants").

- **v0 → v1 (initial schema)** — created by `persistence.py:Persistence._init_schema()` on first run.
- *(no migrations yet)*

When you need to migrate:

1. Add a subsection here with the SQL.
2. Bump a `schema_version` row in a `meta` table (add the table when needed).
3. Run the SQL in `Persistence._init_schema()` guarded by a version check.

---

## Observability

Every `Loop.run` ends in exactly one of `{final, max_turns, stuck_loop, budget,
timeout, error}` (I10). Optional `--trace-dir <path>` (or `LONGAI_TRACE_DIR`
env) dumps per-run artifacts: system prompt, input, envelope, full message
history, tool-call sequence.
