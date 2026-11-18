"""Mark prompt regions that PII redaction leaves alone.

PII protection is about the user's prompt and the data the agent reads. The
context the platform assembles around them — org instructions, schemas, the
agent roster, skills, tool and resource descriptions, saved-query definitions,
the planner's own system directives — is not personal data, and redacting it
breaks the model's work: an instruction "exclude test@corp.com accounts" that
reaches the model as "exclude [REDACTED_EMAIL] accounts" produces a wrong query.

Prompt builders wrap those regions with :func:`pii_context` at the point where
the prompt is assembled. The LLM chokepoint then redacts everything *outside*
the markers and strips the markers before anything is sent
(:func:`strip_markers` runs on every outbound call, PII on or off).

Fail-closed by construction: text nobody marked is redacted. A new prompt path,
or a marker cut off by truncation, can only cause *more* redaction, never less.

The markers are two Unicode private-use code points: they never occur in real
text, survive f-strings and JSON-free concatenation unchanged, and cost one
character each. They are only ever added while assembling an LLM prompt — never
in shared context formatters whose output is also shown in the UI or returned
by the MCP ``get_context`` tool.
"""

from __future__ import annotations

from typing import Any, List, Tuple

CONTEXT_START = ""
CONTEXT_END = ""


def pii_context(text: Any) -> Any:
    """Wrap ``text`` as context that PII redaction leaves unchanged.

    Non-string or empty values pass through, so callers can wrap optional
    blocks without a guard. Markers already inside ``text`` are removed first:
    regions are flat, never nested.
    """
    if not isinstance(text, str) or not text:
        return text
    return f"{CONTEXT_START}{strip_markers(text)}{CONTEXT_END}"


def has_markers(text: Any) -> bool:
    return isinstance(text, str) and (CONTEXT_START in text or CONTEXT_END in text)


def strip_markers(text: Any) -> Any:
    """Remove every context marker. Non-strings pass through."""
    if not isinstance(text, str) or not has_markers(text):
        return text
    return text.replace(CONTEXT_START, "").replace(CONTEXT_END, "")


def split_segments(text: str) -> List[Tuple[bool, str]]:
    """Split ``text`` into ``(is_context, segment)`` pairs, markers removed.

    A region opens at ``CONTEXT_START`` and closes at the next ``CONTEXT_END``.
    Fail-closed handling of malformed input:

    * a start with no matching end (e.g. a truncated prompt) does not open a
      region — the rest of the text is redactable;
    * an end with no open region is dropped;
    * a second start inside an open region is dropped (regions are flat).
    """
    if not has_markers(text):
        return [(False, text)] if text else []
    segments: List[Tuple[bool, str]] = []
    buf: List[str] = []
    i, n = 0, len(text)
    while i < n:
        start = text.find(CONTEXT_START, i)
        end_stray = text.find(CONTEXT_END, i)
        # A stray end before the next start is dropped.
        if end_stray != -1 and (start == -1 or end_stray < start):
            buf.append(text[i:end_stray])
            i = end_stray + 1
            continue
        if start == -1:
            buf.append(text[i:])
            break
        close = text.find(CONTEXT_END, start + 1)
        if close == -1:
            # Unterminated region: treat the remainder as redactable.
            buf.append(text[i:start])
            buf.append(text[start + 1:].replace(CONTEXT_START, ""))
            break
        buf.append(text[i:start])
        if buf:
            segments.append((False, "".join(buf)))
            buf = []
        segments.append((True, text[start + 1:close].replace(CONTEXT_START, "")))
        i = close + 1
    if buf:
        segments.append((False, "".join(buf)))
    return [(ctx, seg) for ctx, seg in segments if seg]
