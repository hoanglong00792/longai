#!/usr/bin/env python3
"""Tiny OpenRouter-shape stub for tests.

Listens on http://localhost:9999/v1/chat/completions.
Returns canned responses keyed by SHA-256 hash of the user message.

Edit RESPONSES at the bottom to add new fixtures.
"""
from __future__ import annotations

import hashlib
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def _key(messages: list[dict]) -> str:
    user = next((m["content"] for m in messages if m["role"] == "user"), "")
    return hashlib.sha256(user.encode()).hexdigest()[:16]


# Response key → (text, tool_calls)
RESPONSES: dict[str, tuple[str, list | None]] = {
    # Add fixtures by user-message hash; default for unknown is "ok"
}


def _build_response(model: str, text: str, tool_calls: list | None) -> dict:
    msg = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
        msg["content"] = ""
    return {
        "id": "chatcmpl-stub", "object": "chat.completion",
        "created": 0, "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
    }


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        messages = body.get("messages", [])
        model = body.get("model", "stub-model")
        k = _key(messages)
        text, tcalls = RESPONSES.get(k, ("ok", None))
        resp = _build_response(model, text, tcalls)
        out = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a, **kw):
        return  # quiet


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"llmstub listening on http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
