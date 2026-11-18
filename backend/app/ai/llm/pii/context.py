"""Mark prompt regions that PII redaction leaves alone.

PII protection is about the user's prompt and the data the agent reads. The
context the platform assembles around them — org instructions, schemas, the
agent roster, skills, tool and resource descriptions, saved-query definitions,
the planner's own system directives — is not personal data, and redacting it
breaks the model's work: an instruction "exclude test@corp.com accounts" that
reaches the model as "exclude [REDACTED_EMAIL] accounts" produces a wrong query.

Prompt builders wrap those regions with :func:`pii_context` at the point where
the prompt is assembled. The LLM chokepoint then redacts everything *outside*
the marked regions and strips the markers before anything is sent
(:func:`strip_markers` runs on every outbound call, PII on or off).

**The text cannot mark itself.** User messages and tool results are
interpolated into the same strings, so the marker characters alone prove
nothing: a region is honored only if it carries a valid HMAC of its exact
content, made with a key that exists only in this process's memory. The
decision to skip scanning therefore rests on that key, not on anything the
prompt text contains. Text that merely includes the reserved characters — typed
by a user, returned by a tool, read from a file — is never skipped: the stray
characters are dropped and the text is scanned like any other.

Fail-closed by construction: text nobody marked is redacted, and so is a region
whose content changed after marking (truncation, editing) because its HMAC no
longer matches. A new prompt path, a cut-off marker or a forged one can only
cause *more* redaction, never less.

The markers are only ever added while assembling an LLM prompt — never in
shared context formatters whose output is also shown in the UI or returned by
the MCP ``get_context`` tool.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any, List, Tuple

# Unicode private-use code points: they never occur in real text.
CONTEXT_START = ""
CONTEXT_END = ""
_MAC_CHARS = 32  # hex characters of HMAC-SHA256 kept (128 bits)

# Per-process key: prompts are assembled and redacted in the same process, and
# the key is never written anywhere.
_KEY = os.urandom(32)


def _mac(text: str) -> str:
    return hmac.new(_KEY, text.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()[:_MAC_CHARS]


def pii_context(text: Any) -> Any:
    """Wrap ``text`` as context that PII redaction leaves unchanged.

    Non-string or empty values pass through, so callers can wrap optional
    blocks without a guard. Markers already inside ``text`` are removed first:
    regions are flat, never nested.
    """
    if not isinstance(text, str) or not text:
        return text
    inner = strip_markers(text)
    return f"{CONTEXT_START}{inner}{CONTEXT_END}{_mac(inner)}"


def has_markers(text: Any) -> bool:
    return isinstance(text, str) and (CONTEXT_START in text or CONTEXT_END in text)


def strip_markers(text: Any) -> Any:
    """Remove every context marker (and the HMAC of each genuine region).
    Non-strings pass through."""
    if not isinstance(text, str) or not has_markers(text):
        return text
    return "".join(segment for _is_context, segment in split_segments(text))


def split_segments(text: str) -> List[Tuple[bool, str]]:
    """Split ``text`` into ``(is_context, segment)`` pairs, markers removed.

    A region is ``START content END hmac``. It is context only when the HMAC
    matches the content. Everything else is in scope, with any reserved
    characters dropped:

    * a start with no end, or an end with no start (a truncated prompt, a
      stray character in user text);
    * a region whose HMAC is missing or wrong (forged, or edited after it was
      marked) — its content is scanned like any other text;
    * a start inside an open region (regions are flat).
    """
    if not has_markers(text):
        return [(False, text)] if text else []
    segments: List[Tuple[bool, str]] = []
    buf: List[str] = []

    def plain(piece: str) -> None:
        if piece:
            buf.append(piece.replace(CONTEXT_START, "").replace(CONTEXT_END, ""))

    def flush() -> None:
        joined = "".join(buf)
        buf.clear()
        if joined:
            segments.append((False, joined))

    i, n = 0, len(text)
    while i < n:
        start = text.find(CONTEXT_START, i)
        if start == -1:
            plain(text[i:])
            break
        plain(text[i:start])
        close = text.find(CONTEXT_END, start + 1)
        if close == -1:
            # Unterminated: the rest is in scope.
            plain(text[start + 1:])
            break
        content = text[start + 1:close]
        tag = text[close + 1:close + 1 + _MAC_CHARS]
        if CONTEXT_START not in content and len(tag) == _MAC_CHARS and hmac.compare_digest(tag, _mac(content)):
            flush()
            segments.append((True, content))
            i = close + 1 + _MAC_CHARS
        else:
            # Not a region the platform marked: drop the start character and
            # keep scanning from just after it (the content stays in scope).
            i = start + 1
    flush()
    return segments
