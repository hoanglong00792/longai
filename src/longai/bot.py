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

from longai.cli import BASE_PROMPT, SAFETY_BLOCK, _build_stack, _skill_catalog
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
        history = p.load_history(chat_id)
        catalog = await _skill_catalog(mcp)
        sysprompt = mem.build_system_prompt(
            chat_id=chat_id, base_prompt=BASE_PROMPT,
            safety_block=SAFETY_BLOCK, skill_catalog=catalog,
        )
        res = await loop.run(
            chat_id=chat_id, system_prompt=sysprompt,
            user_message=user, history=history,
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

    @_auth(cfg.allowed_chat_ids)
    async def on_help(update, context):
        await update.message.reply_text(
            "longai — send any message and I'll respond.\n"
            "/help — this message"
        )

    app = (
        ApplicationBuilder()
        .token(cfg.telegram_bot_token)
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], on_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

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
