# src/longai/bot.py
"""Telegram bot surface (polling, single-process).

Conventions:
- Auth decorator that compares update.effective_user.id (chat_id-equivalent)
- Polling mode for v1 (no public IP needed)

Every reply runs through I11 sanitizer.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from functools import wraps

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from longai import enrichment, fast_commands, router
from longai import tier_classifier
from longai.cli import (
    BASE_PROMPT, SAFETY_BLOCK, _build_stack, _resolve_catalog_for_tier,
)
from longai.config import load
from longai.security import sanitize_outbound

logger = logging.getLogger("longai.bot")


def _is_whitelisted(chat_id: int, allowed: list[int]) -> bool:
    return chat_id in allowed


def _sanitize_reply(text: str) -> str:
    return sanitize_outbound(text)


def _auth(allowed: list[int]):
    def deco(fn):
        @wraps(fn)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if update.effective_chat is None:
                return
            cid = update.effective_chat.id
            if not _is_whitelisted(cid, allowed):
                # F22 — silent ignore (no leak about whitelist size)
                return
            return await fn(update, context)
        return wrapper
    return deco


def run_bot(args: argparse.Namespace) -> int:
    asyncio.run(_run_bot_async(args))
    return 0


async def _run_bot_async(args: argparse.Namespace) -> None:
    cfg, p, mem, mcp, loop = await _build_stack(args.config, require_telegram=True)

    @_auth(cfg.allowed_chat_ids)
    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        import time
        import uuid
        chat_id = update.effective_chat.id
        msg = update.effective_message
        # F21 — voice/image rejected with text reply
        if msg is None or (msg.text is None):
            await msg.reply_text("Voice/image inputs not supported in v1. Send text.")
            return
        user = msg.text
        started_ts = int(time.time())

        # NOTE: fast slash commands (/price, /ta, /caps) are wired as separate
        # Telegram CommandHandlers below — they never reach on_message because
        # the MessageHandler filter excludes commands. Only free-form text
        # falls through here, where it gets pre-LLM enrichment + loop.run.

        # Pre-LLM enrichment for symbol-referencing messages
        hints = router.classify(user)
        ctx_block = await enrichment.enrich(hints, mcp)
        enriched_user = enrichment.attach(user, ctx_block)

        tier, enriched_user = tier_classifier.classify(enriched_user)

        history = p.load_history(chat_id)
        catalog = await _resolve_catalog_for_tier(
            prompt=enriched_user, tier=tier, mcp=mcp,
        )
        sysprompt = mem.build_system_prompt(
            chat_id=chat_id, base_prompt=BASE_PROMPT,
            safety_block=SAFETY_BLOCK, skill_catalog=catalog, tier=tier,
        )
        res = await loop.run(
            chat_id=chat_id, system_prompt=sysprompt,
            user_message=enriched_user, history=history, tier=tier,
        )
        reply = _sanitize_reply(res.text)
        await msg.reply_text(reply)
        p.append_message(chat_id, "user", user, tokens=res.prompt_tokens)
        p.append_message(chat_id, "assistant", res.text, tokens=res.completion_tokens)
        p.log_trace(
            run_id=str(uuid.uuid4()), chat_id=chat_id, started_ts=started_ts,
            stopped=res.stopped, spend_usd=res.spend_usd, turns=res.turns,
            error=res.error,
        )

    def _make_fast_handler(handler_fn):
        """Wrap a fast_commands handler as a Telegram CommandHandler.

        Logs to persistence the same way on_message does, so /price use shows
        up in message history and traces.
        """
        @_auth(cfg.allowed_chat_ids)
        async def telegram_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
            import time
            import uuid
            chat_id = update.effective_chat.id
            arg = " ".join(context.args) if context.args else ""
            started_ts = int(time.time())
            fr = await handler_fn(arg, mcp, p)
            await update.message.reply_text(_sanitize_reply(fr.text))
            cmd_name = update.message.text.split()[0] if update.message and update.message.text else ""
            p.append_message(chat_id, "user", update.message.text or cmd_name, tokens=0)
            p.append_message(chat_id, "assistant", fr.text, tokens=0)
            p.log_trace(
                run_id=str(uuid.uuid4()), chat_id=chat_id, started_ts=started_ts,
                stopped="final", spend_usd=0.0, turns=0, error=fr.error,
            )
        return telegram_handler

    app = (
        ApplicationBuilder()
        .token(cfg.telegram_bot_token)
        .build()
    )
    # Free-form text → on_message (pre-LLM enrichment + agent loop)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    # Slash commands → fast handlers (no LLM, no spend)
    app.add_handler(CommandHandler("price", _make_fast_handler(fast_commands.cmd_price)))
    app.add_handler(CommandHandler("ta", _make_fast_handler(fast_commands.cmd_ta)))
    app.add_handler(CommandHandler("caps", _make_fast_handler(fast_commands.cmd_caps)))
    # /start and /help share the same help text (CLI parity via fast_commands)
    app.add_handler(CommandHandler(["start", "help"], _make_fast_handler(fast_commands.cmd_help)))

    logger.info("longai bot starting; whitelist=%s", cfg.allowed_chat_ids)
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await mcp.stop(); p.close()
