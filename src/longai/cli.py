# src/longai/cli.py
"""CLI subcommand handlers. Same code path as bot.py — both call Loop.run."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from longai.budget_guard import BudgetGuard
from longai.config import ConfigError, load
from longai.envelope import format_error, format_result
from longai.loop import Loop
from longai.mcp_client import MCPRegistry
from longai.memory import Memory
from longai.persistence import Persistence
from longai.security import sanitize_outbound
from longai.trace import Tracer


BASE_PROMPT = (
    "You are longai, a personal assistant. Be concise. Cite tool results when used. "
    "Never reveal private keys or seed phrases. If you're not sure, say so."
)
SAFETY_BLOCK = (
    "NEVER include full wallet addresses, private keys, or seed phrases in replies. "
    "If a tool returns one, the system will mask it before the user sees it, but you "
    "should not echo such values explicitly."
)


def _print_to_user(text: str) -> None:
    """I11 — every CLI print goes through sanitize_outbound."""
    print(sanitize_outbound(text))


async def _build_stack(config_path: str, *, require_telegram: bool):
    cfg = load(config_path, require_telegram=require_telegram)
    p = Persistence(cfg.db_path); p.init()
    mem = Memory(p)
    guard = BudgetGuard(
        api_key=cfg.openrouter_api_key, base_url=cfg.openrouter_base_url,
        models=cfg.models, caps=cfg.caps, persistence=p,
    )
    # Allowlist comes from a static set for v1; future: per-config
    allowlist = {
        "echo", "calculate",
        "eth_call", "eth_getBalance", "eth_getCode", "eth_getStorageAt", "etherscan_get_abi",
        "send_message",
        "recall_memory", "save_memory",
        "list_skills", "load_skill",
        "coingecko_token_info", "dexscreener_pairs", "combined_token_analysis",
        "analyze_ticker",
        # tradingview-mcp allowlist (5 tools)
        "get_technical_analysis", "get_candlestick_patterns",
        "market_snapshot", "financial_news", "market_sentiment",
        # @playwright/mcp tools (Microsoft official, runs via `npx -y @playwright/mcp@latest`)
        "browser_navigate", "browser_navigate_back", "browser_click", "browser_hover",
        "browser_type", "browser_press_key", "browser_select_option", "browser_drag",
        "browser_fill_form", "browser_file_upload", "browser_handle_dialog",
        "browser_snapshot", "browser_take_screenshot", "browser_evaluate",
        "browser_wait_for", "browser_console_messages", "browser_network_requests",
        "browser_resize", "browser_tabs", "browser_close", "browser_run_code",
    }
    mcp = MCPRegistry(cfg.mcp_config_path, allowlist=allowlist)
    await mcp.start()
    loop = Loop(guard=guard, mcp=mcp, max_turns=cfg.caps.per_call_max_turns)
    return cfg, p, mem, mcp, loop


def cmd_dryrun(args: argparse.Namespace) -> int:
    """Validate config + spawn MCPs + exit. NEVER calls OpenRouter."""
    try:
        cfg = load(args.config, require_telegram=False)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    print(f"config OK: {args.config}")
    print(f"  models: {cfg.models}")
    print(f"  caps: ${cfg.caps.global_daily_usd}/day global, ${cfg.caps.per_user_daily_usd}/user")
    print(f"  db: {cfg.db_path}")
    print(f"  mcp config: {cfg.mcp_config_path}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    return asyncio.run(_run_async(args))


async def _run_async(args: argparse.Namespace) -> int:
    prompt = " ".join(args.prompt)
    tracer = Tracer(args.trace_dir)
    try:
        cfg, p, mem, mcp, loop = await _build_stack(args.config, require_telegram=False)
    except ConfigError as e:
        print(json.dumps(format_error(e, trace_id=tracer.run_id)))
        return 1

    try:
        history = p.load_history(args.user_id, max_msgs=20, max_tokens=8000)
        catalog = await _skill_catalog(mcp)
        sysprompt = mem.build_system_prompt(
            chat_id=args.user_id, base_prompt=BASE_PROMPT,
            safety_block=SAFETY_BLOCK, skill_catalog=catalog,
        )
        tracer.system(sysprompt)
        tracer.input(prompt)
        result = await loop.run(
            chat_id=args.user_id, system_prompt=sysprompt,
            user_message=prompt, history=history,
        )
        envelope = format_result(result, model=result.model_used, trace_id=tracer.run_id)
        envelope["result"] = sanitize_outbound(envelope["result"])  # I11
        tracer.output(envelope)
        print(json.dumps(envelope))
        # Persist
        p.append_message(args.user_id, "user", prompt, tokens=result.prompt_tokens)
        p.append_message(args.user_id, "assistant", result.text, tokens=result.completion_tokens)
        return 0 if result.stopped == "final" else 1
    finally:
        await mcp.stop()
        p.close()


async def _skill_catalog(mcp: MCPRegistry) -> str:
    """Try to call list_skills via MCP. Empty string if not available."""
    try:
        result = await mcp.call("list_skills", {})
        d = json.loads(result)
        skills = d.get("skills", [])
        if not skills:
            return ""
        lines = [f"- {s['name']}: {s['description']}" for s in skills[:60]]
        return "\n".join(lines)
    except Exception:
        return ""


def cmd_chat(args: argparse.Namespace) -> int:
    return asyncio.run(_chat_async(args))


async def _chat_async(args: argparse.Namespace) -> int:
    cfg, p, mem, mcp, loop = await _build_stack(args.config, require_telegram=False)
    print("longai chat — type a message or /exit to quit")
    try:
        while True:
            try:
                user = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if user in ("/exit", "/quit"):
                break
            if not user:
                continue
            history = p.load_history(args.user_id)
            catalog = await _skill_catalog(mcp)
            sysprompt = mem.build_system_prompt(
                chat_id=args.user_id, base_prompt=BASE_PROMPT,
                safety_block=SAFETY_BLOCK, skill_catalog=catalog,
            )
            res = await loop.run(
                chat_id=args.user_id, system_prompt=sysprompt,
                user_message=user, history=history,
            )
            _print_to_user(res.text)
            p.append_message(args.user_id, "user", user, tokens=res.prompt_tokens)
            p.append_message(args.user_id, "assistant", res.text, tokens=res.completion_tokens)
    finally:
        await mcp.stop(); p.close()
    return 0


def cmd_bot(args: argparse.Namespace) -> int:
    """Run Telegram bot. Implementation in bot.py."""
    from longai.bot import run_bot
    return run_bot(args)


def cmd_test(args: argparse.Namespace) -> int:
    """Run golden prompts. Implementation in scripts/test_e2e.sh — invoked here."""
    import os
    import subprocess
    extra_env = {"LONGAI_LIVE": "1"} if args.live else {}
    env = {**os.environ, **extra_env}
    return subprocess.call(["bash", "scripts/test_e2e.sh"], env=env)


def cmd_learn(args: argparse.Namespace) -> int:
    return asyncio.run(_learn_async(args))


async def _learn_async(args: argparse.Namespace) -> int:
    """Memory-proposal daemon. Per I7: propose-don't-apply."""
    import time
    from pathlib import Path

    cfg, p, mem, mcp, loop = await _build_stack(args.config, require_telegram=False)
    try:
        if args.apply:
            return _apply_candidates(p, mem, args.apply)
        last_id = p.cursor_get("memory_proposer")
        msgs = p.messages_since(after_id=last_id)
        if not msgs:
            print("Nothing to learn since last run.")
            return 0
        sample = "\n".join(f"[{m['role']}] {m['content']}" for m in msgs[-50:])
        sysprompt = (
            "You analyze chat transcripts and propose memory entries. "
            "Output ONLY append-only entries in this format, one per line:\n"
            "- [preference] short stable user-pref ≤500 chars\n"
            "- [domain] short factual reference ≤500 chars\n"
            "Skip episodic/procedural content. Output ONLY the bullets, nothing else."
        )
        result = await loop.run(
            chat_id=-2, system_prompt=sysprompt,
            user_message=f"Transcript:\n{sample}",
            history=[],
        )
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        out_path = Path("~/.longai").expanduser() / f"candidates-{ts}.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(result.text)
        new_id = max(m["id"] for m in msgs)
        p.cursor_set("memory_proposer", new_id)
        print(f"Wrote candidates to {out_path}")
        print(f"Review then run: longai learn --apply {out_path}")
        return 0
    finally:
        await mcp.stop(); p.close()


def _apply_candidates(p, mem, path: str) -> int:
    import re
    import time
    from pathlib import Path
    text = Path(path).expanduser().read_text()
    line_re = re.compile(r"^-\s*\[(preference|domain)\]\s+(.+)$")
    inserted = 0
    for line in text.splitlines():
        m = line_re.match(line.strip())
        if not m:
            continue
        mem.save(
            type=m.group(1), content=m.group(2).strip()[:500],
            chat_id=None, source="daemon_proposed", applied_by="user",
            ts=int(time.time()),
        )
        inserted += 1
    print(f"Applied {inserted} candidate(s).")
    return 0
