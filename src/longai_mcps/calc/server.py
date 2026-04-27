"""Calc MCP — ast-based safe arithmetic evaluator. NEVER eval()."""
from __future__ import annotations

import ast
import asyncio
import json
import operator as op
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


_BIN_OPS = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
    ast.FloorDiv: op.floordiv, ast.Mod: op.mod, ast.Pow: op.pow,
}
_UNARY_OPS = {ast.UAdd: op.pos, ast.USub: op.neg}


def safe_eval(expr: str) -> float:
    """Evaluate a numeric expression using AST whitelist. Raises ValueError on bad input."""
    tree = ast.parse(expr, mode="eval")

    def walk(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            return _BIN_OPS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return _UNARY_OPS[type(node.op)](walk(node.operand))
        raise ValueError(f"disallowed expr node: {type(node).__name__}")

    return walk(tree)


server = Server("longai-calc")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(
        name="calculate",
        description="Evaluate a numeric arithmetic expression. Supports + - * / ** and parentheses.",
        inputSchema={
            "type": "object",
            "properties": {"expr": {"type": "string"}},
            "required": ["expr"],
        },
    )]


@server.call_tool()
async def call_tool(name: str, args: dict) -> list[TextContent]:
    if name != "calculate":
        return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]
    try:
        v = safe_eval(args.get("expr", ""))
        return [TextContent(type="text", text=json.dumps({"result": v}))]
    except (ValueError, SyntaxError, ZeroDivisionError) as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
