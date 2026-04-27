# src/longai_mcps/skill_loader/server.py
"""I12 — read-only skill loader. Reads from external skill repos.

Each scope (shared / personal / work) maps to a directory containing skill
markdown files. Scopes are user-defined and configured via env vars:

Env:
  LONGAI_SKILLS_SHARED   (path to "shared" skill repo, optional)
  LONGAI_SKILLS_PERSONAL (path to "personal" skill repo, optional)
  LONGAI_SKILLS_WORK     (path to "work" skill repo, optional)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


BODY_CAP = 8000  # 8KB cap per I12


def _repos() -> list[tuple[str, Path]]:
    """Return (label, path) for each configured skill repo that exists."""
    out: list[tuple[str, Path]] = []
    for label, env in (("shared", "LONGAI_SKILLS_SHARED"),
                        ("personal", "LONGAI_SKILLS_PERSONAL"),
                        ("work", "LONGAI_SKILLS_WORK")):
        v = os.environ.get(env)
        if v:
            p = Path(os.path.expanduser(v))
            if p.exists():
                out.append((label, p))
    return out


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _parse_skill(skill_md: Path, label: str) -> dict[str, Any] | None:
    """Parse YAML-ish frontmatter to (name, description, access, body)."""
    try:
        text = skill_md.read_text()
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    fm_text = m.group(1)
    body = text[m.end():]
    fields: dict[str, str] = {}
    # Quick YAML-ish parser: lines like `key: value` or `key: >` (folded)
    cur_key: str | None = None
    for line in fm_text.splitlines():
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val == ">" or val == "|":
                cur_key = key
                fields[key] = ""
            else:
                fields[key] = val
                cur_key = None
        elif cur_key and line.startswith(" "):
            fields[cur_key] = (fields.get(cur_key, "") + " " + line.strip()).strip()
    name = fields.get("name", skill_md.parent.name)
    desc = fields.get("description", "")
    access = fields.get("access", label)
    return {"name": name, "description": desc, "access": access,
            "repo": label, "body": body.strip(), "path": str(skill_md)}


def _all_skills() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for label, root in _repos():
        skills_dir = root / "skills"
        if not skills_dir.exists():
            continue
        for sk in sorted(skills_dir.iterdir()):
            md = sk / "SKILL.md"
            if md.exists():
                parsed = _parse_skill(md, label)
                if parsed:
                    out.append(parsed)
    return out


def _list_skills_impl(query: str | None, access: str | None) -> dict[str, Any]:
    skills = _all_skills()
    if query:
        q = query.lower()
        skills = [s for s in skills
                  if q in s["name"].lower() or q in s["description"].lower()]
    if access:
        skills = [s for s in skills if s["access"] == access]
    # Trim description for token efficiency
    return {
        "skills": [
            {"name": s["name"], "description": s["description"][:120],
             "access": s["access"], "repo": s["repo"]}
            for s in skills
        ],
    }


def _load_skill_impl(name: str) -> dict[str, Any]:
    for s in _all_skills():
        if s["name"] == name:
            body = s["body"]
            if len(body) > BODY_CAP:
                body = body[:BODY_CAP] + f"\n\n[...skill body truncated, see file at {s['path']} for full text]"
            return {"name": name, "body": body, "path": s["path"]}
    return {"error": f"skill not found: {name}"}


server = Server("longai-skill-loader")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_skills",
            description="List available skill names with truncated descriptions. "
                        "Optional query (substring) and access (shared|personal|work) filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "access": {"type": "string", "enum": ["shared", "personal", "work"]},
                },
            },
        ),
        Tool(
            name="load_skill",
            description="Load the body of a SKILL.md by name (capped at 8KB).",
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, args: dict) -> list[TextContent]:
    if name == "list_skills":
        out = _list_skills_impl(args.get("query"), args.get("access"))
        return [TextContent(type="text", text=json.dumps(out))]
    if name == "load_skill":
        out = _load_skill_impl(args.get("name", ""))
        return [TextContent(type="text", text=json.dumps(out))]
    return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]


async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
