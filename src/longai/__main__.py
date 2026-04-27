# src/longai/__main__.py
"""longai CLI entrypoint.

Subcommands: bot | chat | run | test | dryrun | learn
"""
from __future__ import annotations

import argparse
import sys


_CONFIG_DEFAULT = "~/.longai/config.toml"


def _add_common(sub_p: argparse.ArgumentParser) -> None:
    """Add --config and --trace-dir to a subcommand parser."""
    sub_p.add_argument("--config", default=_CONFIG_DEFAULT,
                       help="Path to config.toml (default: ~/.longai/config.toml)")
    sub_p.add_argument("--trace-dir", default=None,
                       help="Dump per-run artifacts to this dir")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="longai", description="Personal Telegram bot CLI")
    # Keep top-level --config/--trace-dir for backwards compat (e.g. --help)
    p.add_argument("--config", default=_CONFIG_DEFAULT,
                   help="Path to config.toml (default: ~/.longai/config.toml)")
    p.add_argument("--trace-dir", default=None, help="Dump per-run artifacts to this dir")
    sub = p.add_subparsers(dest="cmd", required=False)

    bot_p = sub.add_parser("bot", help="Start the Telegram bot (polling)")
    _add_common(bot_p)

    chat = sub.add_parser("chat", help="Interactive REPL using the agent loop")
    _add_common(chat)
    chat.add_argument("--user-id", type=int, default=-1)

    run = sub.add_parser("run", help="Single-shot prompt; print JSON envelope")
    _add_common(run)
    run.add_argument("prompt", nargs="+")
    run.add_argument("--user-id", type=int, default=-1)

    test_p = sub.add_parser("test", help="Run golden prompts (vs llmstub by default)")
    _add_common(test_p)
    test_p.add_argument("--live", action="store_true", help="Use real OpenRouter free chain")

    dryrun_p = sub.add_parser("dryrun", help="Validate config + spawn MCPs + exit")
    _add_common(dryrun_p)

    learn = sub.add_parser("learn", help="Memory-proposal daemon")
    _add_common(learn)
    learn.add_argument("--apply", default=None, help="Apply candidates from this path")
    learn.add_argument("--since", default="7d")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.cmd or "bot"

    from longai import cli

    handlers = {
        "bot": cli.cmd_bot, "chat": cli.cmd_chat, "run": cli.cmd_run,
        "test": cli.cmd_test, "dryrun": cli.cmd_dryrun, "learn": cli.cmd_learn,
    }
    return handlers[cmd](args)


if __name__ == "__main__":
    sys.exit(main())
