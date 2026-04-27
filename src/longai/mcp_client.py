"""MCP client registry — wraps the official mcp SDK.

Per I1: we do NOT manage subprocesses ourselves; the SDK does.
Timeouts: 30s startup per server, 30s per call.
Allowlist: if provided, only tools in the set are exposed via tools().
F13: startup failure → degraded mode (skip server, don't abort).
F14: tool crash mid-call → return {"error": "tool crashed: <name>: <e>"}.
F15: unknown tool → raise UnknownTool.

Design note: anyio cancel scopes must be entered and exited from the same task.
We therefore keep each server's entire lifecycle (open → use → close) inside a
single anyio background task that blocks on an anyio.Event until stop() is called.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

STARTUP_TIMEOUT_S = 30
PER_CALL_TIMEOUT_S = 30

logger = logging.getLogger(__name__)


class UnknownTool(Exception):
    """Raised when the requested tool is not registered (F15)."""


@dataclass
class _ServerEntry:
    name: str
    session: ClientSession
    tool_names: set[str]
    tool_specs: list[Any]  # list of mcp Tool objects


class MCPRegistry:
    """Registry of MCP servers loaded from a JSON config file.

    Args:
        config_path: path to a JSON file with ``{"mcpServers": {...}}``.
        allowlist: if not None, only tools whose names are in this set
                   are exposed via ``tools()``.
    """

    def __init__(
        self,
        config_path: str,
        allowlist: set[str] | None = None,
    ) -> None:
        import os
        self._config_path = Path(os.path.expanduser(config_path))
        self._allowlist = allowlist
        self._servers: list[_ServerEntry] = []
        # Background asyncio task running the anyio event loop work
        self._bg_task: asyncio.Task | None = None
        # anyio.Event set when stop() is called — set inside the anyio task
        self._stop_event: anyio.Event | None = None
        # asyncio.Event signalling that startup is complete
        self._ready: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to all configured servers. Degraded on per-server failure.

        If config file is missing, runs with no servers (degraded mode).
        """
        if not self._config_path.exists():
            logger.warning(
                "MCP config not found at %s — running with no servers",
                self._config_path,
            )
            return
        config = json.loads(self._config_path.read_text())
        servers_cfg: dict[str, Any] = config.get("mcpServers", {})

        self._ready = asyncio.Event()
        loop = asyncio.get_running_loop()
        self._bg_task = loop.create_task(
            self._anyio_main(servers_cfg), name="mcp_registry_bg"
        )

        # Wait for all startup tasks to finish (bounded by STARTUP_TIMEOUT_S + buffer)
        try:
            await asyncio.wait_for(
                asyncio.shield(self._ready.wait()),
                timeout=STARTUP_TIMEOUT_S + 5,
            )
        except asyncio.TimeoutError:
            logger.warning("MCPRegistry startup wait timed out")

    async def stop(self) -> None:
        """Shut down all servers."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._bg_task is not None:
            self._bg_task.cancel()
            try:
                await self._bg_task
            except (asyncio.CancelledError, Exception):
                pass
        self._servers.clear()

    def tools(self) -> list[dict[str, Any]]:
        """Return tool descriptors in OpenAI function-calling shape (filtered by allowlist)."""
        out: list[dict[str, Any]] = []
        for entry in self._servers:
            for tspec in entry.tool_specs:
                if self._allowlist is not None and tspec.name not in self._allowlist:
                    continue
                out.append({
                    "type": "function",
                    "function": {
                        "name": tspec.name,
                        "description": tspec.description or "",
                        "parameters": tspec.inputSchema or {"type": "object"},
                    },
                })
        return out

    async def call(self, name: str, args: dict[str, Any]) -> str:
        """Call a tool by name.

        Raises:
            UnknownTool: if no server exposes *name* (F15).

        Returns:
            JSON string with tool output, or ``{"error": "..."}`` on failure (F14).
        """
        session: ClientSession | None = None
        for entry in self._servers:
            if name in entry.tool_names:
                if self._allowlist is not None and name not in self._allowlist:
                    continue
                session = entry.session
                break

        if session is None:
            raise UnknownTool(f"no registered MCP tool named {name!r}")

        try:
            result = await asyncio.wait_for(
                session.call_tool(name, arguments=args),
                timeout=PER_CALL_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return json.dumps({"error": f"tool timeout: {name}"})
        except Exception as exc:
            return json.dumps({"error": f"tool crashed: {name}: {exc}"})

        parts = []
        for c in result.content:
            text = getattr(c, "text", None)
            if text is not None:
                parts.append(text)
        return "\n".join(parts) if parts else "{}"

    # ------------------------------------------------------------------
    # Internal background runner (anyio)
    # ------------------------------------------------------------------

    async def _anyio_main(self, servers_cfg: dict[str, Any]) -> None:
        """Runs inside an asyncio task; uses anyio to manage server lifecycles."""
        self._stop_event = anyio.Event()
        ready_events: list[anyio.Event] = []

        try:
            async with anyio.create_task_group() as tg:
                for server_name, cfg in servers_cfg.items():
                    server_ready = anyio.Event()
                    ready_events.append(server_ready)
                    tg.start_soon(
                        self._server_lifecycle, server_name, cfg, server_ready
                    )

                # Wait for all per-server tasks to finish startup
                for ev in ready_events:
                    await ev.wait()

                # Signal the outer asyncio world that startup is complete
                asyncio.get_running_loop().call_soon_threadsafe(self._ready.set)

                # Block here until stop() is called
                await self._stop_event.wait()

                # Cancel all server tasks
                tg.cancel_scope.cancel()
        except Exception as exc:
            logger.error("MCPRegistry background error: %s", exc)
        finally:
            asyncio.get_running_loop().call_soon_threadsafe(self._ready.set)

    async def _server_lifecycle(
        self,
        server_name: str,
        cfg: dict,
        ready: anyio.Event,
    ) -> None:
        """Open one MCP server, register its tools, then block until cancelled."""
        params = StdioServerParameters(
            command=cfg["command"],
            args=cfg.get("args", []),
            env=cfg.get("env") or None,
        )

        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    with anyio.fail_after(STARTUP_TIMEOUT_S):
                        await session.initialize()
                        list_result = await session.list_tools()

                    tool_specs = list_result.tools
                    tool_names = {t.name for t in tool_specs}
                    self._servers.append(
                        _ServerEntry(
                            name=server_name,
                            session=session,
                            tool_names=tool_names,
                            tool_specs=tool_specs,
                        )
                    )
                    ready.set()

                    # Hold open until the task group is cancelled
                    await anyio.sleep_forever()

        except anyio.get_cancelled_exc_class():
            # Normal cancellation on stop()
            raise
        except Exception as exc:
            logger.warning(
                "MCP server %r failed to start (degraded mode): %s",
                server_name,
                exc,
            )
        finally:
            ready.set()  # Ensure startup barrier is always released
