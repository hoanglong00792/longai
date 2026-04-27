# `longai`

A personal Telegram bot and local CLI that holds a conversation with you,
runs tools when it needs to, and stays under a dollar a day. It picks free
models from OpenRouter automatically, falls back through a chain when one
goes down, and never silently overspends.

It looks like this:

```
$ longai run "what's ETH trading at"
~$3,420 (CoinGecko, 12s ago). 24h: +2.1%.

$ longai chat
> summarize https://vitalik.eth.limo/general/2024/12/01/scaling.html in 3 bullets
1) ...
2) ...
3) ...
```

Built for one user (you) and up to ten trusted friends. Not a public bot.

---

## Quick Start (5 minutes, no Telegram needed)

You need a free OpenRouter key. Nothing else is mandatory.

### 1. Prerequisites

```bash
brew install uv          # Python package manager (required)
brew install node        # for browser automation (recommended)
```

### 2. Clone and set up the venv

```bash
git clone https://github.com/hoanglong00792/longai.git && cd longai
bash scripts/setup_venv.sh     # creates .venv, installs deps (~30s)
```

### 3. Config and MCP registry

```bash
mkdir -p ~/.longai
cp config.example.toml ~/.longai/config.toml
bash scripts/init_mcp.sh       # generates ~/.longai/mcp.json with correct paths
```

### 4. API key — store it once, every shell sees it

Get a free key at [openrouter.ai](https://openrouter.ai) (no credit card needed
for free models). Drop it into `~/.longai/env` — the `longai` wrapper
auto-sources this file before launching, so you don't need to export anything
in your shell rc:

```bash
cp env.example ~/.longai/env
chmod 600 ~/.longai/env
$EDITOR ~/.longai/env          # set OPENROUTER_API_KEY=sk-or-v1-...
```

If you'd rather export it the old way: `export OPENROUTER_API_KEY=sk-or-v1-...`.
The env-file flow is what makes the global CLI install (next section) work in
fresh terminals.

### 5. Run it

```bash
./longai dryrun            # sanity check — prints config + model chain, no API calls
./longai run "what's 2+2"  # single shot
./longai chat              # interactive REPL
```

That's it. The bot picks from a chain of free OpenRouter models automatically.

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

---

## Adding optional capabilities

| Capability | What to do |
|---|---|
| **Telegram bot** | Set `TELEGRAM_BOT_TOKEN=...` and add your chat ID to `allowed_chat_ids` in `config.toml`, then `./longai bot` |
| **Web search** | Set `TAVILY_API_KEYS=tvly-...` (1000 free searches/month at [tavily.com](https://tavily.com)); without it the agent falls back to Playwright |
| **Browser automation** | Install Node.js ≥ 18 (`brew install node`) — enables 21 `browser_*` tools via Playwright |
| **On-chain ABI decoding** | Set `ETHERSCAN_API_KEY=...` |
| **Custom RPC** | Set `LONGAI_RPC_URL_ETHEREUM=https://...` etc. — defaults use LlamaRPC |

Full details, alternative providers (local Ollama), and troubleshooting in
[`SETUP.md`](./SETUP.md).

---

## Daily commands

```bash
./longai run "<prompt>"      # single-shot — text reply when interactive, JSON when piped
./longai run --json "..."    # force JSON envelope (model, turns, spend, trace_id)
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

`learn` proposes memory entries from your conversation history but never
applies them automatically — you review and approve.

---

## Tests

```bash
arch -arm64 .venv/bin/python -m pytest -q                          # unit tests
bash scripts/check_i3.sh                                           # I3 chokepoint enforcement
bash scripts/live_replay.sh --tier 1                               # 2 smoke cases against real API
bash scripts/live_replay.sh --reset-cooldowns --throttle 10        # full sweep
bash scripts/live_replay.sh --ids smoke_math,smoke_greeting        # re-run specific cases
```

Live-replay cases live in `tests/e2e/test_prompts.json`, organized by tier —
including multi-tool composition exercises that probe the agent's exploration
ability.

---

## How it's wired

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the MCP toolbelt, tiered
model routing, persistence layout, and observability.

The twelve non-negotiable rules — what won't change, ever — are in
[`INVARIANTS.md`](./INVARIANTS.md).
