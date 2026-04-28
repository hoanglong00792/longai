# Changelog

All notable changes to this project. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project does **not** follow strict semver (it's a personal harness, not a
library), but breaking config-shape or invariant changes will be called out
explicitly under their version.

## [Unreleased]

## [0.2.0] — 2026-04-27 / 2026-04-28

A latency-and-correctness sprint. The agent loop went from "5-turn ETH price
queries" to "1-turn with measured timing." Adds tiered model routing, slash
commands that bypass the loop, pre-LLM data enrichment, and per-phase trace
timing.

### Added

- **Tiered model routing** ([`444638f`]). `BudgetGuard.chat(tier=)` selects
  the model chain based on a regex classifier (`/quick`, `/deep` prefixes,
  research keywords). New `[models.tier_S/M/L]` + `[models.fallback]` config
  schema; `[caps.tier_*]` overrides for tier-specific `max_turns` /
  `wall_clock_s`. Legacy `models = [...]` still works.
- **Tier-aware refresh** ([`43f659d`]). `scripts/refresh_models.sh --tiered`
  fetches free OpenRouter models, auto-classifies them by parameter count
  parsed from the slug, applies user overrides from
  `~/.longai/model_tiers.local.toml`, and writes the tiered config blocks.
  Idempotent on re-run.
- **Pre-LLM enrichment + slash-command bypass** ([`4e8cf0f`], [`d3aa82b`]).
  `/price <symbol>`, `/ta`, `/caps`, `/help` skip the agent loop entirely.
  Free-form messages with detected ticker symbols, contract addresses
  (`0x…40hex`), or URLs get pre-fetched data prepended to the user message
  with a no-re-fetch directive. All enrichments run concurrently.
- **Skill complexity tier-bump** ([`ef016b4`]). `SKILL.md` frontmatter accepts
  optional `complexity: S | M | L`. When the agent calls `load_skill`, the
  loop bumps tier upward if the loaded skill is heavier than the current
  tier. Never downgrades.
- **`longai run` clean-text default** ([`76df08c`]). When stdout is a TTY,
  prints just the reply. JSON envelope when piped. Explicit `--json` /
  `--text` flags override.
- **Per-phase timing in trace + envelope** ([`d953e69`]). New
  `06_timings.jsonl` per run-dir with `chat`, `tool`, `turn`,
  `enrich.{market,contract,url}`, `request.total` events. Envelope gains
  top-level `latency_ms`. `live_replay.sh`'s `01_summary.tsv` adds a
  `latency_ms` column.
- **Optional example skill installer** ([`92af77d`]).
  `scripts/install_skills.example.sh` copies a curated allowlist of skills
  from a sibling skill repo into `~/.longai/skills-shared/skills/` so the
  `skill_loader` MCP exposes only an agent-relevant cut.
- **`~/.longai/env` auto-source** ([`e67780d`]). The `longai` wrapper
  sources this dotenv-style file before launching, so global-CLI invocations
  pick up `OPENROUTER_API_KEY` etc. without shell-rc edits. New
  `env.example` template.

### Changed

- **README opens with what the bot does** ([`b526316`]). Two example
  conversations precede the install steps. Heavy details (MCP toolbelt,
  Ollama setup, arch flags) moved to `ARCHITECTURE.md` and `SETUP.md` to
  reduce first-time-reader friction.
- **`ARCHITECTURE.md`** (new, [`b526316`]). MCP toolbelt, tiered routing,
  persistence layout, observability — separated from the README.

### Fixed

- **`/price` and market enrichment were silent no-ops** ([`ca3e367`]). The
  on_chain_ta MCP's `coingecko_token_info` is contract-based — needs
  `(chain, address)` — but the call site passed `{"symbol": "ETH"}`. Every
  call 404'd silently, no enrichment ever fired, and the LLM had to figure
  things out via tools (often hitting `max_turns`). New `prices.py` calls
  CoinGecko's `simple/price` endpoint directly via httpx with a curated
  symbol→coin-id map covering `ASSET_MAP`. Live-verified: "what's ETH
  trading at" exits in 1 turn (was 5).
- **README install URL placeholder** ([`05e3a0f`]). Replaced `<repo-url>`
  with the actual GitHub URL.

### Internal

- 261 unit tests (was 113 at session start), I3 chokepoint check still
  passing.
- Added `live_replay.sh` `turns_max` expect-block assertion.
- Tiered e2e cases: `tier_quick_routing`, `tier_default_routing`,
  `tier_deep_routing`, `fast_help`, `fast_price_eth`,
  `enriched_eth_price_query`, `enriched_eth_price_short`.

[`92af77d`]: https://github.com/hoanglong00792/longai/commit/92af77d
[`e67780d`]: https://github.com/hoanglong00792/longai/commit/e67780d
[`b526316`]: https://github.com/hoanglong00792/longai/commit/b526316
[`05e3a0f`]: https://github.com/hoanglong00792/longai/commit/05e3a0f
[`444638f`]: https://github.com/hoanglong00792/longai/commit/444638f
[`43f659d`]: https://github.com/hoanglong00792/longai/commit/43f659d
[`4e8cf0f`]: https://github.com/hoanglong00792/longai/commit/4e8cf0f
[`ef016b4`]: https://github.com/hoanglong00792/longai/commit/ef016b4
[`76df08c`]: https://github.com/hoanglong00792/longai/commit/76df08c
[`d3aa82b`]: https://github.com/hoanglong00792/longai/commit/d3aa82b
[`ca3e367`]: https://github.com/hoanglong00792/longai/commit/ca3e367
[`d953e69`]: https://github.com/hoanglong00792/longai/commit/d953e69

## [0.1.0] — 2026-04-27 (and earlier)

Initial v1 release. Personal Telegram bot + local CLI on a free-models-first
fallback chain through OpenRouter. Twelve non-negotiable invariants enforced
(see [`INVARIANTS.md`](./INVARIANTS.md)). 113 unit tests.

Pre-CHANGELOG history: see `git log` for granular changes; `INVARIANTS.md`
documents what won't change.
