# longai INVARIANTS

> **These are non-negotiable.** Every PR, every doc, every refactor passes through this list.
> If you're tempted to violate one, **fork the repo**. Don't bend.
>
> **Cited from:** `README.md`, `docs/superpowers/specs/2026-04-27-longai-design.md`,
> every component header docstring in `src/longai/`, every test file, `pyproject.toml`'s
> long_description, and `MIGRATIONS.md`. **If you change this file, search-and-replace
> the citations to match.**

---

## I1 — Don't reinvent the wheel

Use existing libraries: `python-telegram-bot`, `openai` SDK, official `mcp` Python SDK,
`sqlite3` stdlib, `tradingview-mcp` (atilaahmettaner/tradingview-mcp). We do **not**
fork Claude Code, we do **not** write our own MCP protocol, we do **not** write our
own subprocess manager. Existing Playwright MCP + KyberSwap MCPs are reused as-is.

## I2 — Free models first

Default model chain (in order):

```
1. google/gemma-4-26b-a4b-it:free                          ← primary
2. google/gemma-4-31b-it:free                              ← first fallback
3. nvidia/nemotron-3-super-120b-a12b:free                  ← capability headroom
4. google/gemma-4-26b-a4b-it                               ← PAID floor (~$0.06/$0.33 per M)
```

Never pin a paid model as primary. If a free slug stops being free, the chain falls
through; **budget-guard catches the dollars.** Paid Gemma-4 (~$0.06/$0.33 per M) is
~13× cheaper than Haiku-4.5; we use that as the floor, not Anthropic.

## I3 — Single chokepoint to OpenRouter (chat completion)

**ONE module** — `src/longai/budget_guard.py` — owns the OpenAI client and the
`chat/completions` HTTP path. No other module constructs an OpenAI client or
calls chat/completions directly.

**Allowed elsewhere:** read-only metadata calls like `GET /api/v1/models`
(used by `models_cache.py` for free-model discovery). These don't spend money,
don't pass through caps, and don't need the chokepoint.

Enforced by CI grep (`scripts/check_i3.sh`):

```bash
! grep -rn -E 'openai\.AsyncOpenAI|openai\.OpenAI|chat/completions' \
    src/longai/ src/longai_mcps/ --exclude=budget_guard.py
```

This is the "separate guard component" promise without a process boundary.
The discipline is convention + grep, not a language guarantee — **document loud,
test loud.**

## I4 — Strict budget caps, locked numbers

| Cap | Value | Behavior on breach |
|---|---|---|
| **Global / day** | **$1.00** | Hard kill all new calls until UTC midnight |
| **Per-user / day** | **$0.25** | Hard kill that user; other users keep going |
| **Per-call max spend** | **$0.025** | Abort that call mid-turn; return error envelope |
| **Per-call wall-clock** | **30 seconds** | SIGTERM the call, return timeout error |
| **Per-call max turns** | **5** | Stop loop, return accumulated text |

Day-roll is implicit (counters are sqlite queries scoped by `day_utc`, not stored values).
Caps live in `config.toml` as defaults; overriding them requires editing config —
**they are not silently overridable from the CLI per-call.**

## I5 — Local CLI is non-optional

The Telegram bot and the CLI share the same `Loop.run` code path. Three CLI
subcommands ship in v1:

```
longai chat              interactive REPL (Telegram-without-Telegram)
longai run "<prompt>"    single-shot, JSON envelope to stdout
longai test              golden prompts vs llmstub (--live for real OR free chain)
longai dryrun            validate config + spawn MCPs + exit (no LLM call)
longai learn             scan history, propose memory candidates (see I7)
```

This invariant exists because the user said: *"we must have a way to test it
quickly in local without using telegram, and can easily trigger to test the whole
system when we change something."* The CLI is **not optional** and **must use the
same code path** as the Telegram surface (no parallel implementations).

## I6 — Personal use only (was P1)

This bot is for the maintainer and **≤10 trusted `chat_id`s.** Public-bot mode is **FORBIDDEN.**

If you want to open it to strangers, **FORK.** Do not remodel — the architecture
trades public-bot ergonomics for personal simplicity, non-reversibly. Indicators
that you've crossed the line and need to stop:

- Adding per-user paywalls or rate limits beyond budget caps
- Adding a "register" or "invite" flow
- Removing the `allowed_chat_ids` whitelist
- Adding abuse mitigation, captcha, or anti-bot measures

If you find yourself doing any of the above, **stop the PR and fork.**

## I7 — Propose-don't-apply for memory (was M1)

The bot **never silently writes** to long-term memory. The `memories` table has:

```sql
applied_by TEXT NOT NULL CHECK (applied_by IN ('user', 'agent'))
```

Every memory entry is either:
- (a) saved by the agent via the `save_memory` MCP tool (`source='agent_tool'`,
  `applied_by='agent'`, **visible in trace**), or
- (b) approved by you via `longai learn --apply <candidates.md>` after manual review
  (`source='daemon_proposed'`, `applied_by='user'`).

**No code path writes `applied_by='daemon'`.** Sqlite CHECK constraint enforces it.

## I8 — Outbound chat_id whitelist

The `telegram_out` MCP tool (`src/longai_mcps/telegram_out/server.py`) checks every
destination `chat_id` against `allowed_outbound_chat_ids` from `config.toml` **at the
tool layer.** Unauthorized destinations return:

```json
{"error": "chat_id not in outbound whitelist"}
```

before any Telegram API call is made.

Unit test `tests/test_outbound_whitelist.py` proves this. **The check is at the tool
layer, not at the LLM layer**, so even a model that hallucinates the wrong destination
cannot leak.

## I9 — Token-rename in envelope only

OpenRouter returns `usage.prompt_tokens` / `usage.completion_tokens` (OpenAI shape).
The Anthropic-style rename to `input_tokens` / `output_tokens` happens in
`src/longai/envelope.py:format_result()` and `format_error()`.

**Not** in `budget_guard.py` (which keeps OpenAI shape internally). **Not** in
`loop.py`. Asserted in `tests/test_envelope.py` on **every error path**, not just
the happy path.

Doyen has flagged this rename three times across docs. Three.

## I10 — No silent failures

Every `Loop.run` ends in **exactly one** of `{final, max_turns, stuck_loop, budget,
timeout, error}`. The user always gets a reply (Telegram message or CLI stdout).
**Silent stop is a bug** and a ship-blocker.

If you ever observe "the bot just stopped replying," that's a code defect, not a
missing feature.

## I11 — Output sanitizer in the Telegram/CLI path

Every outbound message — `bot.py:send_reply()` and `cli.py:print_to_user()` —
runs through `security.sanitize_outbound(text)` before reaching the user.
The sanitizer enforces:

| Rule | Action |
|---|---|
| Full wallet addresses (`0x` + 40 hex) | Mask to `0x1234…abcd` (first 6, last 4) |
| 64-char hex strings (potential private keys) | Replace with `[PRIVATE KEY REDACTED]` |
| 12-or-24-word BIP-39 mnemonic phrases | Replace with `[SEED PHRASE REDACTED]` |
| RPC URLs containing API keys (Infura/Alchemy/QuickNode) | Mask the key portion |
| Specific portfolio %, allocations, perf numbers from private-profile | Suppressed (per `longai-skills-shared/hooks/privacy-guard.sh`) |

Sourced from an internal privacy-guard hook and a battle-tested regex bank
maintained outside this repo. Unit-tested with adversarial fixtures in
`tests/test_security.py`. **The sanitizer runs on every path — it is not a
CLI-flag, not skippable, not configurable per-call.**

## I12 — Skills are read-only references, not silently executed

The bot reuses skills from `longai-skills-{shared,personal,work}` repos via:

1. The `skill_loader` MCP tool — `list_skills(query)` returns names+descriptions;
   `load_skill(name)` returns the SKILL.md body (capped 8 KB).
2. Direct MCP wrappers for skills with executable Python (v1 list:
   `on_chain_ta`, `longai_strategy`).

The bot does **not** execute SKILL.md bodies as code, does **not** fork Claude
Code's hook/skill execution model, and does **not** modify any file under
`longai-skills-*`. Skill repos are **read-only sources of truth.**

If a skill has executable code that the bot needs (Category A in
`docs/superpowers/specs/2026-04-27-longai-design.md`), wrap it as its own MCP
server under `src/longai_mcps/`. Don't import skill scripts directly — that
couples the bot to skill-repo internals.

---

## On adding new invariants

If you discover a new non-negotiable while working on this project:

1. Number it `I11`, `I12`, ... (never reuse a retired number).
2. Add it to this file with the same section structure: rule, citation, enforcement.
3. Search-and-replace the citation list at the top of this file to add the new doc(s).
4. Add or update a unit test that proves the invariant.
5. Commit with `invariant: add I11 — <one-line rule>`.

## On retiring an invariant

If reality changes (e.g., user count grows past 10 → I6 doesn't apply because you
forked anyway), retire by:

1. **Strike through** the rule — leave it in the file with `~~text~~` so the history
   survives.
2. Add `**RETIRED YYYY-MM-DD: <reason>.**` underneath.
3. Do **not** renumber the surviving invariants.

---

*Last updated: 2026-04-27.*
