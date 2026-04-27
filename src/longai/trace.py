# src/longai/trace.py
"""Trace dumper. No-op when trace_dir is None."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


class Tracer:
    def __init__(self, trace_dir: str | None):
        self._dir: Path | None = None
        self._run_id = str(uuid.uuid4())
        if trace_dir:
            base = Path(os.path.expanduser(trace_dir))
            ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            self._dir = base / f"{ts}_{os.getpid()}_{self._run_id[:8]}"
            self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def run_id(self) -> str:
        return self._run_id

    def meta(self, d: dict[str, Any]) -> None:
        if self._dir is None: return
        # Redact sensitive keys
        clean = {k: ("[REDACTED]" if k.lower().endswith("token") or k.lower().endswith("key") else v)
                 for k, v in d.items()}
        (self._dir / "00_meta.json").write_text(json.dumps(clean, indent=2))

    def input(self, prompt: str) -> None:
        if self._dir is None: return
        (self._dir / "01_input.txt").write_text(prompt)

    def system(self, s: str) -> None:
        if self._dir is None: return
        (self._dir / "02_system_prompt.txt").write_text(s)

    def message(self, m: dict) -> None:
        if self._dir is None: return
        with (self._dir / "03_messages.jsonl").open("a") as f:
            f.write(json.dumps(m) + "\n")

    def output(self, env: dict) -> None:
        if self._dir is None: return
        (self._dir / "07_output.json").write_text(json.dumps(env, indent=2))

    def timing(self, phase: str, ms: float, **extra: Any) -> None:
        """Append a timing record. Phase is a short label like 'chat',
        'tool', 'enrich.market'. ``ms`` is wall-clock; extra kwargs add
        context (model, name, turn, symbol). No-op when trace_dir is None.
        """
        if self._dir is None: return
        rec: dict[str, Any] = {"phase": phase, "ms": round(float(ms), 1),
                               "ts": time.time()}
        rec.update(extra)
        with (self._dir / "06_timings.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")
