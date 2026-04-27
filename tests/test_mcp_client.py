"""Tests for MCPRegistry (Task 10)."""
import json
import sys
from pathlib import Path

import pytest

from longai.mcp_client import MCPRegistry, UnknownTool

# Path to the echo MCP server fixture
FIXTURE_DIR = Path(__file__).parent / "fixtures"
ECHO_SERVER = FIXTURE_DIR / "echo_mcp.py"


def _make_mcp_config(tmp_path: Path) -> Path:
    """Write a temporary mcp.json using the current Python interpreter."""
    cfg = {
        "mcpServers": {
            "echo": {
                "command": sys.executable,
                "args": [str(ECHO_SERVER)],
            }
        }
    }
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps(cfg))
    return p


@pytest.fixture
async def registry(tmp_path):
    """Start an MCPRegistry against the echo server and stop it after the test."""
    cfg = _make_mcp_config(tmp_path)
    reg = MCPRegistry(config_path=str(cfg), allowlist=None)
    await reg.start()
    yield reg
    await reg.stop()


@pytest.fixture
async def registry_allowlisted(tmp_path):
    """MCPRegistry with an allowlist that excludes the echo tool."""
    cfg = _make_mcp_config(tmp_path)
    reg = MCPRegistry(config_path=str(cfg), allowlist={"nonexistent_tool"})
    await reg.start()
    yield reg
    await reg.stop()


async def test_start_lists_tools(registry):
    """After start(), tools() returns at least the echo tool in OpenAI shape."""
    tools = registry.tools()
    assert any(t["function"]["name"] == "echo" for t in tools)


async def test_call_returns_text(registry):
    """call("echo", {"text": "hello world"}) returns a JSON string containing the text."""
    result = await registry.call("echo", {"text": "hello world"})
    assert "hello world" in result


async def test_allowlist_filters_tools(registry_allowlisted):
    """When allowlist excludes echo, tools() does not contain echo."""
    tools = registry_allowlisted.tools()
    assert not any(t["function"]["name"] == "echo" for t in tools)


async def test_unknown_tool_raises(registry):
    """Calling an unknown tool raises UnknownTool (F15)."""
    with pytest.raises(UnknownTool):
        await registry.call("no_such_tool", {})
