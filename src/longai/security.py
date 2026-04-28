"""I11 — Output sanitizer.

Sourced from an internal privacy-guard hook and a battle-tested regex bank
maintained outside this repo.

The sanitizer runs on every outbound path; it is not a CLI flag, not skippable,
not configurable per-call.
"""
from __future__ import annotations

import re
from functools import lru_cache

# Wallet address — 0x followed by exactly 40 hex chars (must NOT be 64-char keys)
_WALLET = re.compile(r"\b0x([a-fA-F0-9]{6})[a-fA-F0-9]{30}([a-fA-F0-9]{4})\b")
# Private key — 64-char hex (with optional 0x prefix)
_PRIVKEY = re.compile(r"\b(?:0x)?[a-fA-F0-9]{64}\b")
# RPC URL with embedded API key (Infura / Alchemy / QuickNode patterns)
_ALCHEMY = re.compile(r"(alchemy\.com/v2)/[A-Za-z0-9_-]{8,}")
_INFURA = re.compile(r"(infura\.io/v3)/[A-Za-z0-9_-]{8,}")
_QUICKNODE = re.compile(r"(quiknode\.pro/[a-zA-Z0-9-]+/)[a-zA-Z0-9_-]{8,}")
# BIP-39 word list (canonical 2048 words). Loaded lazily.
# 12 or 24 BIP-39 words separated by single spaces.

# Prompt-injection signals in tool outputs
_INJECT_TAGS = re.compile(r"<system-reminder>|</system-reminder>|<important>|</important>", re.IGNORECASE)


@lru_cache(maxsize=1)
def _bip39_words() -> set[str]:
    """English BIP-39 wordlist. Hardcoded subset of common words for v1.

    For v2: download the full 2048-word list from
    https://github.com/bitcoin/bips/blob/master/bip-0039/english.txt
    and embed it.
    """
    # The 2048-word BIP-39 list, abbreviated to known prefixes for v1.
    # Real implementation should bundle the full file.
    common = """abandon ability able about above absent absorb abstract absurd abuse
access accident account accuse achieve acid acoustic acquire across act action actor
actress actual adapt add addict address adjust admit adult advance advice aerobic
affair afford afraid again age agent agree ahead aim air airport aisle alarm album
alcohol alert alien all alley allow almost alone alpha already also alter always amateur
amazing among amount amused analyst anchor ancient anger angle angry animal ankle
announce annual another answer antenna antique anxiety any apart apology appear apple
approve april arch arctic area arena argue arm armed armor army around arrange arrest
arrive arrow art artefact artist artwork ask aspect assault asset assist assume asthma
athlete atom attack attend attitude attract auction audit august aunt author auto
autumn average avocado avoid awake aware away awesome awful awkward axis
""".split()
    return set(common)


def _is_bip39_phrase(words: list[str]) -> bool:
    """Check if every word in the list is in the BIP-39 wordlist."""
    if len(words) not in (12, 24):
        return False
    bip = _bip39_words()
    return all(w.lower() in bip for w in words)


def _redact_seeds(text: str) -> str:
    """Find runs of 12 or 24 BIP-39 words and replace with marker."""
    # Tokenize on whitespace, scan for runs of bip39 words.
    tokens = re.findall(r"\S+|\s+", text)
    out: list[str] = []
    i = 0
    while i < len(tokens):
        if not tokens[i].strip():
            out.append(tokens[i])
            i += 1
            continue
        # Try a 24-word run first, then 12-word
        for length in (24, 12):
            words: list[str] = []
            j = i
            while j < len(tokens) and len(words) < length:
                if tokens[j].strip():
                    # strip surrounding punctuation for matching
                    w = re.sub(r"[^a-zA-Z]", "", tokens[j])
                    if w:
                        words.append(w)
                j += 1
            if len(words) == length and _is_bip39_phrase(words):
                out.append("[SEED PHRASE REDACTED]")
                i = j
                break
        else:
            out.append(tokens[i])
            i += 1
    return "".join(out)


def _mask_wallet(match: re.Match) -> str:
    head = match.group(1)
    tail = match.group(2)
    return f"0x{head}…{tail}"


def sanitize_outbound(text: str) -> str:
    """Sanitize text before sending to user (Telegram or CLI).

    Applied to every reply. Not configurable per-call.
    """
    if not text:
        return text
    # Order matters: redact seed phrases before private keys
    # (so a 64-char hex inside a seed-phrase context doesn't double-redact).
    out = _redact_seeds(text)
    out = _PRIVKEY.sub("[PRIVATE KEY REDACTED]", out)
    out = _WALLET.sub(_mask_wallet, out)
    out = _ALCHEMY.sub(r"\1/[KEY-MASKED]", out)
    out = _INFURA.sub(r"\1/[KEY-MASKED]", out)
    out = _QUICKNODE.sub(r"\1[KEY-MASKED]", out)
    return out


class StreamSanitizer:
    """Incremental sanitizer for streaming model output.

    Every secret pattern we care about (wallet, private-key, RPC URL,
    seed phrase) is *atomic* — it fits on a single line. So we emit only
    complete lines: each line is sanitized in full before any of it
    leaves this object. The trailing partial line is held until ``flush``.

    Trade-off: streaming granularity is line, not token. For a single
    long paragraph the user only sees output once it's complete. In
    practice the model emits paragraph breaks (\\n\\n) and the user
    sees output as paragraphs land, which is plenty interactive.

    Usage:
        s = StreamSanitizer()
        for chunk in stream:
            piece = s.feed(chunk)
            if piece:
                print(piece, end="", flush=True)
        print(s.flush(), end="", flush=True)
    """

    def __init__(self) -> None:
        self._buffer: str = ""
        self._emitted_buffer_chars: int = 0

    def feed(self, chunk: str) -> str:
        """Append a chunk; return any newly-completed sanitized lines."""
        if not chunk:
            return ""
        self._buffer += chunk
        last_nl = self._buffer.rfind("\n", self._emitted_buffer_chars)
        if last_nl < 0:
            return ""
        end = last_nl + 1  # include the newline
        full = sanitize_outbound(self._buffer[:end])
        prev = sanitize_outbound(self._buffer[:self._emitted_buffer_chars])
        self._emitted_buffer_chars = end
        return full[len(prev):]

    def flush(self) -> str:
        """End-of-stream: sanitize and emit any trailing partial line."""
        if self._emitted_buffer_chars >= len(self._buffer):
            return ""
        full = sanitize_outbound(self._buffer)
        prev = sanitize_outbound(self._buffer[:self._emitted_buffer_chars])
        self._emitted_buffer_chars = len(self._buffer)
        return full[len(prev):]


def sanitize_tool_output(text: str) -> str:
    """Sanitize tool-call output before feeding into next-turn context.

    Defends against prompt injection (wraps suspicious content) AND
    runs the same wallet/key/seed redaction so the model never sees raw
    secrets that might be echoed back to the user.
    """
    if not text:
        return text
    text = sanitize_outbound(text)  # mask + redact
    if _INJECT_TAGS.search(text):
        text = f"[UNTRUSTED INPUT]\n{text}\n[/UNTRUSTED INPUT]"
    return text
