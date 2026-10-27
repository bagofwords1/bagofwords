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
GREP_WINDOW_BEFORE = 200  # of a long-line window's GREP_LINE_CLIP chars, how many precede the match
GREP_WINDOW_MIN_AFTER = 150  # trailing context a later match needs inside a window to share it


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
    A line longer than GREP_LINE_CLIP yields one match per window around its
    occurrences instead (see _line_windows); `col_start`/`col_end`/`line_length`
    mark it, and the before/after context rides on the line's first window only.
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
        ctx_start = max(0, i - before)
        if len(line) <= GREP_LINE_CLIP:
            entries = [{
                "line_no": i + 1,
                "line": line,
                "before": lines[ctx_start:i],
                "after": lines[i + 1:i + 1 + after],
                "context_start_line": ctx_start + 1,
            }]
        else:
            entries = []
            for n, (start, end) in enumerate(_line_windows(rx, line)):
                first = n == 0
                entries.append({
                    "line_no": i + 1,
                    "line": line[start:end],
                    "before": lines[ctx_start:i] if first else [],
                    "after": lines[i + 1:i + 1 + after] if first else [],
                    "context_start_line": ctx_start + 1 if first else i + 1,
                    "col_start": start,
                    "col_end": end,
                    "line_length": len(line),
                })
        total += len(entries)
        matches.extend(entries[:max(0, max_matches - len(matches))])
    return matches, total, total > len(matches)


def _line_windows(rx: re.Pattern, line: str) -> list[tuple[int, int]]:
    """[start, end) windows of at most GREP_LINE_CLIP chars over a long line.

    One window per occurrence, opening GREP_WINDOW_BEFORE chars ahead of it —
    a head-of-line clip hid matches past the first GREP_LINE_CLIP chars while
    still reporting the line as a hit, and generated JSX often keeps a whole
    section on one line. Near the end of the line the window slides back to
    stay full width, unless that would repeat more of the previous window than
    its lead-in. An occurrence gets no window of its own when the
    previous one already shows it along with GREP_WINDOW_MIN_AFTER chars of
    what follows (or the rest of the line) — what follows a match is usually
    what an edit needs, so a match in the last few chars of a window does not
    count as shown.
    """
    windows: list[tuple[int, int]] = []
    for m in rx.finditer(line):
        if windows:
            prev_end = windows[-1][1]
            if m.end() <= prev_end and (prev_end - m.end() >= GREP_WINDOW_MIN_AFTER or prev_end == len(line)):
                continue
        # A long occurrence gets less lead-in so the window opens on it.
        start = m.start() - min(GREP_WINDOW_BEFORE, max(0, GREP_LINE_CLIP - (m.end() - m.start())))
        start = max(0, min(start, len(line) - GREP_LINE_CLIP))
        if windows:
            # Overlap the previous window by at most the lead-in (dense
            # occurrences, or the slide-back at the line end), so a frequent
            # word doesn't repeat each char several times over.
            start = max(start, min(m.start(), prev_end - GREP_WINDOW_BEFORE))
        windows.append((start, min(len(line), start + GREP_LINE_CLIP)))
    return windows


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
