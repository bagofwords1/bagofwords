"""Windowed reads of one instruction's text, shared by every read_instruction.

The in-app agent's read_instruction tool and the MCP read_instruction tool
both return instruction text to a model whose transport caps one result: the
agent transcript cuts a tool result at 100k chars, and MCP clients cap or
drop large tool results (Claude Code keeps ~25k tokens). A body that doesn't
fit is therefore never returned whole and cut on the way — it comes back as a
window that says it is partial and how to read on:

- plain read: the whole text when it fits, otherwise the opening lines plus a
  line-numbered outline ('head', ``truncated`` + ``next_offset``);
- range read: lines ``offset``..``offset+limit-1`` within the budget; a line
  too long for one read comes in pieces (``col_offset`` continues it);
- grep read: matching lines with context (``_text_window.grep_lines``).

Range and grep text is verbatim, so a caller can anchor an edit on it.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from app.ai.tools.implementations._text_window import (
    DEFAULT_RANGE_LIMIT,
    grep_lines,
    outline_from_patterns,
)

# Budgets are measured on the JSON-encoded text (ASCII-escaped), because that
# is how the result travels: a newline costs 2 there and a non-ASCII character
# up to 6. Keeping every read under these budgets means a read is never
# silently cut on its way to the model.
FULL_READ_MAX_ENCODED_CHARS = 40_000   # plain read returns the whole body up to this
HEAD_MAX_ENCODED_CHARS = 16_000        # opening lines shown when the body is too long
WINDOW_MAX_ENCODED_CHARS = 40_000      # cap for one range read / one grep read
OUTLINE_MAX_ENCODED_CHARS = 20_000
OUTLINE_MAX_ENTRIES = 200

# Landmarks in instruction bodies: markdown headings, YAML/`key:` lines at the
# top level, nested map headers (`  some_event:`), and list entries that name
# themselves (`- name: ...`).
_OUTLINE_PATTERNS = [
    re.compile(r"^#{1,6}\s+\S"),
    re.compile(r"^[\w\"'][^:#]{0,100}:(\s|$)"),
    re.compile(r"^\s{1,4}[\w\"'.-][^:#]{0,100}:\s*$"),
    re.compile(r"^\s*-\s+(?:name|event|event_name|id|key|title)\s*:", re.IGNORECASE),
]


def _encoded_len(value: Any) -> int:
    return len(json.dumps(value, default=str))


def _fit(text: str, avail: int) -> str:
    """Longest prefix of `text` whose JSON-encoded body fits `avail` (min 1 char)."""
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _encoded_len(text[:mid]) - 2 <= avail:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def _window(
    lines: List[str], start: int, max_lines: int, budget: int, col: int = 0,
) -> Tuple[str, int, Optional[Tuple[int, int, int]]]:
    """Lines from 1-based `start` (its first line from char `col`), at most
    `max_lines`, within `budget`.

    Returns (text, end_line, cut). Always returns something so a caller paging
    on makes progress. A line too long for the whole budget (rare: one giant
    line, e.g. a pasted JSON blob) is returned in pieces: `cut` is then
    (line_no, line_length, next_col) — read on from that line at `next_col`.
    Nothing follows a cut piece: it would read as following on from it.
    """
    total = len(lines)
    out: List[str] = []
    used = 2  # the JSON string quotes
    idx = start - 1
    cut: Optional[Tuple[int, int, int]] = None
    while idx < total and len(out) < max_lines:
        line = lines[idx][col:] if not out else lines[idx]
        cost = _encoded_len(line) - 2 + (2 if out else 0)  # + escaped newline
        if out and used + cost > budget:
            break
        if not out and used + cost > budget:
            line = _fit(line, budget - used)
            cost = _encoded_len(line) - 2
            cut = (idx + 1, len(lines[idx]), col + len(line))
        out.append(line)
        used += cost
        idx += 1
        if cut:
            break
    return "\n".join(out), start + len(out) - 1, cut


def _bounded_outline(text: str) -> str:
    outline = outline_from_patterns(
        text, _OUTLINE_PATTERNS, max_entries=OUTLINE_MAX_ENTRIES, entry_clip=120
    )
    if _encoded_len(outline) <= OUTLINE_MAX_ENCODED_CHARS:
        return outline
    kept: List[str] = []
    used = 2
    for entry in outline.split("\n"):
        cost = _encoded_len(entry)
        if used + cost > OUTLINE_MAX_ENCODED_CHARS:
            kept.append("     … outline cut to fit — use grep_pattern to locate entries")
            break
        kept.append(entry)
        used += cost
    return "\n".join(kept)


def _bounded_matches(matches: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    """Drop trailing matches past WINDOW_MAX_ENCODED_CHARS. Returns (kept, dropped_any)."""
    kept: List[Dict[str, Any]] = []
    used = 2
    for m in matches:
        cost = _encoded_len(m)
        if kept and used + cost > WINDOW_MAX_ENCODED_CHARS:
            return kept, True
        kept.append(m)
        used += cost
    return kept, False


def _paging(fields: Dict[str, Any], end: int, total_lines: int, cut, col: int) -> str:
    """Set truncated/next_offset (and the long-line fields) on a line window;
    return the summary tail telling the reader how to continue."""
    if col:
        fields["start_col"] = col
    if cut:
        line_no, length, next_col = cut
        fields.update(truncated=True, next_offset=line_no, next_col_offset=next_col,
                      clipped_line=line_no, clipped_line_length=length)
        return (
            f" Line {line_no} is {length:,} chars — too long for one read: this shows chars "
            f"{col:,}-{next_col:,} of it. Read on with offset={line_no}, col_offset={next_col} "
            "(that text continues this one with no line break), or use grep_pattern."
        )
    fields["truncated"] = end < total_lines
    if end < total_lines:
        fields["next_offset"] = end + 1
        return f" Continue with offset={end + 1}."
    return ""


def read_window(
    full_text: str,
    title: str,
    *,
    offset: Optional[int] = None,
    col_offset: Optional[int] = None,
    limit: Optional[int] = None,
    grep_pattern: Optional[str] = None,
    ignore_case: bool = True,
    before: int = 0,
    after: int = 0,
    max_matches: int = 50,
) -> Dict[str, Any]:
    """Pick the read mode and return {"text", "fields", "summary"}.

    grep > range > full; a plain read of a body over the budget returns the
    opening lines plus an outline ('head') instead of the whole text. Full
    reads return just the text (no extra fields). Raises re.error for an
    invalid grep pattern.
    """
    total_chars = len(full_text)
    lines = full_text.split("\n")
    total_lines = len(lines)
    base = {"total_chars": total_chars, "total_lines": total_lines}

    if grep_pattern:
        matches, total, more = grep_lines(
            full_text,
            grep_pattern,
            ignore_case=ignore_case,
            before=before,
            after=after,
            max_matches=max_matches,
        )
        matches, dropped = _bounded_matches(matches)
        fields = {
            **base,
            "read_mode": "grep",
            "matches": matches,
            "grep_total_matches": total,
            "grep_truncated": more or dropped,
        }
        if total == 0:
            fields["outline"] = _bounded_outline(full_text)
            summary = (
                f"0 matches for {grep_pattern!r} in instruction '{title}' "
                f"({total_lines:,} lines). An outline with line numbers is included — "
                "pick a range (offset/limit) or a better pattern."
            )
        else:
            summary = f"{total} match(es) for {grep_pattern!r} in instruction '{title}'"
            if fields["grep_truncated"]:
                summary += f", showing {len(matches)} — narrow the pattern or raise max_matches for the rest"
            summary += ". Matched lines and context are verbatim."
        return {"text": "", "fields": fields, "summary": summary}

    if offset is not None or limit is not None or col_offset:
        start = offset or 1
        if start > total_lines:
            fields = {**base, "read_mode": "range", "start_line": start,
                      "end_line": start - 1, "truncated": False}
            summary = (
                f"offset {start} is past the end of instruction '{title}' "
                f"({total_lines:,} lines). Retry with a smaller offset."
            )
            return {"text": "", "fields": fields, "summary": summary}
        col = min(col_offset or 0, len(lines[start - 1]))
        text, end, cut = _window(
            lines, start, limit or DEFAULT_RANGE_LIMIT, WINDOW_MAX_ENCODED_CHARS, col
        )
        fields = {**base, "read_mode": "range", "start_line": start, "end_line": end}
        summary = f"Read instruction '{title}' lines {start}-{end} of {total_lines:,}."
        summary += _paging(fields, end, total_lines, cut, col)
        return {"text": text, "fields": fields, "summary": summary}

    if _encoded_len(full_text) <= FULL_READ_MAX_ENCODED_CHARS:
        return {"text": full_text, "fields": {}, "summary": f"Read instruction '{title}'"}

    text, end, cut = _window(lines, 1, total_lines, HEAD_MAX_ENCODED_CHARS)
    fields = {
        **base,
        "read_mode": "head",
        "start_line": 1,
        "end_line": end,
        "outline": _bounded_outline(full_text),
    }
    summary = (
        f"Instruction '{title}' is too long to return whole ({total_chars:,} chars, "
        f"{total_lines:,} lines): showing lines 1-{end} plus an outline. "
        "Do NOT treat this as the complete text. Use grep_pattern to pull the entries "
        "you need, or read on."
    )
    summary += _paging(fields, end, total_lines, cut, 0)
    fields["truncated"] = True
    return {"text": text, "fields": fields, "summary": summary}
