"""Line-window helpers shared by the read tools that page long text.

read_artifact (dashboard code / doc markdown) and read_instruction (instruction
and skill bodies) both need the same three reads over a long body: a 1-based
line range, a line-level regex grep with context, and a line-numbered outline
to navigate by. Range slices and grep lines are returned VERBATIM (no line
number prefixes) so callers can quote them in anchored edits.
"""

import re
from collections.abc import Sequence
from typing import Any

DEFAULT_RANGE_LIMIT = 400
GREP_LINE_CLIP = 500


def slice_range(text: str, offset: int, limit: int | None) -> tuple[str, int, int, int, bool]:
    """Return (slice_text, start_line, end_line, total_lines, truncated) for a
    1-based line range read. The slice is verbatim (no line-number prefixes)."""
    lines = text.split("\n")
    total_lines = len(lines)
    limit = limit or DEFAULT_RANGE_LIMIT
    start = max(1, offset)
    if start > total_lines:
        return "", start, start - 1, total_lines, False
    end = min(total_lines, start + limit - 1)
    sliced = "\n".join(lines[start - 1:end])
    return sliced, start, end, total_lines, end < total_lines


def grep_lines(
    text: str,
    pattern: str,
    ignore_case: bool = False,
    before: int = 0,
    after: int = 0,
    max_matches: int = 50,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Grep text line-by-line.

    Returns (matches, total_matches, truncated). Each match carries the verbatim
    matching line plus verbatim before/after context and 1-based line numbers.
    Raises re.error for an invalid pattern — callers surface that to the planner.
    """
    flags = re.IGNORECASE if ignore_case else 0
    rx = re.compile(pattern, flags)
    lines = text.split("\n")
    matches: list[dict[str, Any]] = []
    total = 0
    for i, line in enumerate(lines):
        if not rx.search(line):
            continue
        total += 1
        if len(matches) >= max_matches:
            continue
        ctx_start = max(0, i - before)
        clipped = line if len(line) <= GREP_LINE_CLIP else line[:GREP_LINE_CLIP]
        matches.append({
            "line_no": i + 1,
            "line": clipped,
            "before": lines[ctx_start:i],
            "after": lines[i + 1:i + 1 + after],
            "context_start_line": ctx_start + 1,
        })
    return matches, total, total > len(matches)


def outline_from_patterns(
    text: str,
    patterns: Sequence[re.Pattern],
    *,
    max_entries: int = 300,
    entry_clip: int = 160,
    sample_entries: int = 40,
) -> str:
    """Line-numbered outline of the lines matching any structural pattern.

    Returns lines like `  123: ## Revenue rules`. When nothing structural
    matches, falls back to evenly spaced sample lines so the caller still gets
    landmarks to range-read around.
    """
    lines = text.split("\n")
    entries: list[str] = []
    for i, line in enumerate(lines):
        if any(p.search(line) for p in patterns):
            stripped = line.strip()
            if len(stripped) > entry_clip:
                stripped = stripped[:entry_clip] + "…"
            entries.append(f"{i + 1:>6}: {stripped}")
            if len(entries) >= max_entries:
                entries.append(f"     … outline truncated at {max_entries} entries ({len(lines)} lines total)")
                break
    if not entries:
        step = max(1, len(lines) // sample_entries)
        for i in range(0, len(lines), step):
            stripped = lines[i].strip()
            if stripped:
                entries.append(f"{i + 1:>6}: {stripped[:entry_clip]}")
            if len(entries) >= sample_entries:
                break
    return "\n".join(entries)
