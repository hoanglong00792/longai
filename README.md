# `longai`

Personal Telegram bot for ≤10 trusted users. Free-models-first agent loop on
OpenRouter, with a local CLI for testing without Telegram.

> **Status:** v1 working end-to-end. See [`INVARIANTS.md`](./INVARIANTS.md) for
> the 12 non-negotiable rules.

---

## Quick Start (5 minutes, no Telegram needed)

You will need a free OpenRouter API key. Nothing else is mandatory.

### 1. Prerequisites

```bash
brew install uv          # Python package manager (required)
brew install node        # only if you want browser automation (optional)
```

### 2. Clone and set up the venv

```bash
git clone <repo-url> longai && cd longai
bash scripts/setup_venv.sh     # creates .venv, installs deps (~30s)
```

### 3. Config and MCP registry

```bash
mkdir -p ~/.longai
cp config.example.toml ~/.longai/config.toml
bash scripts/init_mcp.sh       # generates ~/.longai/mcp.json with correct paths
```

### 4. API key

Get a free key at [openrouter.ai](https://openrouter.ai) (no credit card needed for free models):

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
```

### 5. Run it

```bash
./longai dryrun            # sanity check — prints config + model chain, no API calls
./longai run "what's 2+2"  # single shot
./longai chat              # interactive REPL
```

That's it. The bot picks from a chain of free OpenRouter models automatically.

> **Why `./longai` and not `uv run longai`?** On Apple Silicon Macs, `uv run`
> can silently swap arm64 wheels for x86_64 ones, causing `incompatible
> architecture` errors. The `./longai` wrapper forces arm64 and bypasses this.
> Always use `./longai` on macOS.

---

## Install as a global CLI (any terminal)

Pick one — the wrapper resolves its own location, so all three keep working
after `git pull`:

```bash
# 1. Symlink (recommended). Make sure ~/.local/bin is in PATH.
mkdir -p ~/.local/bin && ln -sf "$(pwd)/longai" ~/.local/bin/longai

# 2. Shell alias.
echo "alias longai='$(pwd)/longai'" >> ~/.zshrc && source ~/.zshrc

# 3. PATH the repo directly.
echo "export PATH=\"$(pwd):\$PATH\"" >> ~/.zshrc && source ~/.zshrc
```

Verify from anywhere:

```bash
cd ~ && longai dryrun
```

> **Secrets in a fresh terminal:** the wrapper auto-sources `~/.longai/env`
> before launching, so global-CLI invocations pick up `OPENROUTER_API_KEY`
> etc. without you having to export them in every shell. Copy the template:
> `cp env.example ~/.longai/env && chmod 600 ~/.longai/env`, then edit.

---

## Adding optional capabilities

| Capability | What to do |
|---|---|
| **Telegram bot** | Set `TELEGRAM_BOT_TOKEN=...` and add your chat ID to `allowed_chat_ids` in `config.toml`, then `./longai bot` |
| **Web search** | Set `TAVILY_API_KEYS=tvly-...` (1000 free searches/month at [tavily.com](https://tavily.com)); without it the agent falls back to Playwright |
| **Browser automation** | Install Node.js ≥ 18 (`brew install node`) — enables 21 `browser_*` tools via Playwright |
| **On-chain ABI decoding** | Set `ETHERSCAN_API_KEY=...` |
| **Custom RPC** | Set `LONGAI_RPC_URL_ETHEREUM=https://...` etc. — defaults use LlamaRPC |

Full details in [`SETUP.md`](./SETUP.md).

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

## Daily commands

```bash
./longai run "<prompt>"      # single-shot, prints JSON envelope
./longai chat                # interactive REPL (no Telegram)
./longai bot                 # Telegram polling
./longai test                # golden prompts vs llmstub (cost = $0)
./longai test --live         # golden prompts vs real OpenRouter (~$0.05)
./longai dryrun              # config validation only, no API calls
./longai refresh             # force-refresh the free models cache
./longai refresh --show      # print current cache without refreshing
```

### Advanced

```bash
./longai learn               # propose memory candidates from chat history
./longai learn --apply <p>   # commit reviewed candidates to memory store
```

`learn` proposes memory entries from your conversation history but never applies
them automatically — you review and approve. See [`INVARIANTS.md`](./INVARIANTS.md) (I7).

---

## Alternative provider: local Ollama

You can run the entire bot against a **local Ollama server** instead of
OpenRouter — same code path, different endpoint. Useful for offline or private
chat.

> **Caveat:** small local models handle multi-step tool composition unreliably.
> Use Ollama for casual chat; switch back to OpenRouter for anything that
> chains tools (Playwright research, EVM tracing, etc.).

```bash
brew install ollama && ollama serve &
ollama pull gemma4:e4b
export OPENROUTER_BASE_URL=http://localhost:11434/v1
export OPENROUTER_API_KEY=ollama   # any non-empty string

# Override the model chain
cat >> ~/.longai/config.toml <<'EOF'

[models_refresh]
policy = "manual"

models = ["gemma4:e4b"]
EOF

./longai dryrun    # confirm models = [gemma4:e4b]
./longai run "say hi"
```

To flip back: `unset OPENROUTER_BASE_URL` and remove the override block.

---

## Tests

```bash
arch -arm64 .venv/bin/python -m pytest -q                          # 113 unit tests
bash scripts/check_i3.sh                                            # I3 chokepoint enforcement
bash scripts/live_replay.sh --tier 1                                # 2 smoke cases against real API
bash scripts/live_replay.sh --reset-cooldowns --throttle 10         # full sweep, 20 cases
bash scripts/live_replay.sh --ids smoke_math,smoke_greeting         # re-run specific cases
```

20 live-replay cases across 7 tiers in `tests/e2e/test_prompts.json` — including
multi-tool composition exercises that probe the agent's exploration ability.

---

## Architecture

Telegram + CLI both call `Loop.run`, which calls `BudgetGuard.chat` (the only
chat-completion caller), which iterates a fallback chain of free models with
strict per-call/per-user/global caps.

12 non-negotiable rules: see [`INVARIANTS.md`](./INVARIANTS.md).
