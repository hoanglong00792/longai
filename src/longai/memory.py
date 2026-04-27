"""I7 — long-term memory (propose-don't-apply by sqlite CHECK constraint).

Wraps persistence.memory_* helpers and adds the system-prompt assembly:
  base_prompt + safety_block + preferences (≤1KB) + skill_catalog
"""
from __future__ import annotations

import time

from longai.persistence import Persistence

PREFERENCES_BYTE_CAP = 1024


class Memory:
    def __init__(self, persistence: Persistence):
        self._p = persistence

    def save(
        self, *, type: str, content: str, chat_id: int | None,
        source: str, applied_by: str, ts: int | None = None,
    ) -> int:
        return self._p.memory_save(
            type=type, content=content, source=source, chat_id=chat_id,
            applied_by=applied_by, created_ts=ts if ts is not None else int(time.time()),
        )

    def recall(
        self, *, query: str | None, chat_id: int | None,
        type: str | None = None, limit: int = 5,
    ) -> list[dict]:
        return self._p.memory_recall(query=query, chat_id=chat_id, type=type, limit=limit)

    def load_preferences(self, *, chat_id: int, limit: int = 20) -> str:
        rows = self._p.memory_recall(
            query=None, chat_id=chat_id, type="preference", limit=limit,
        )
        out: list[str] = []
        used = 0
        for r in rows:
            line = f"- {r['content']}"
            if used + len(line) + 1 > PREFERENCES_BYTE_CAP:
                break
            out.append(line)
            used += len(line) + 1
        return "\n".join(out)

    def build_system_prompt(
        self,
        *,
        chat_id: int,
        base_prompt: str,
        safety_block: str,
        skill_catalog: str,
    ) -> str:
        prefs = self.load_preferences(chat_id=chat_id)
        sections = [base_prompt, "## Safety", safety_block]
        if prefs:
            sections.extend(["## What you remember about this user/group", prefs])
        if skill_catalog:
            sections.extend([
                "## Skills available (call load_skill(name) for body)",
                skill_catalog,
                "## Memory available",
                "Call recall_memory(query) for domain knowledge beyond preferences.",
            ])
        return "\n\n".join(sections)
