"""Shared helpers for doc artifacts (create_doc / edit_doc).

A doc's source of truth is a single markdown string. Structure is carried by
convention:
  - ``{{viz:<uuid>}}`` placeholders embed live visualizations
  - fenced ```` ```mermaid ```` blocks embed diagrams
  - ``::: columns`` / ``::: col`` / ``:::`` containers make multi-column layout

These helpers keep placeholder extraction and edit application in one place so
the tools, tests, and any future consumers agree on the exact semantics.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

# Hard cap on document size (characters). Generous for reports, small enough to
# keep tool args, prompt observations and the editor responsive.
MAX_DOC_CHARS = 60_000

_VIZ_PLACEHOLDER_RE = re.compile(
    r"\{\{\s*viz:\s*([0-9a-fA-F-]{8,64})\s*\}\}"
)
_FENCE_RE = re.compile(r"^(```|~~~)", re.MULTILINE)


def _fence_spans(markdown: str) -> List[Tuple[int, int]]:
    """Return [start, end) spans of fenced code blocks (``` or ~~~).

    An unclosed fence extends to the end of the document — placeholders inside
    it are still treated as quoted, not live.
    """
    spans: List[Tuple[int, int]] = []
    open_pos: int | None = None
    open_marker: str | None = None
    for m in _FENCE_RE.finditer(markdown):
        marker = m.group(1)
        if open_pos is None:
            open_pos = m.start()
            open_marker = marker
        elif marker == open_marker:
            # Close the fence at the end of its line
            line_end = markdown.find("\n", m.end())
            end = len(markdown) if line_end == -1 else line_end + 1
            spans.append((open_pos, end))
            open_pos = None
            open_marker = None
    if open_pos is not None:
        spans.append((open_pos, len(markdown)))
    return spans


def _in_spans(pos: int, spans: List[Tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in spans)


def extract_viz_placeholders(markdown: str) -> List[str]:
    """Extract viz UUIDs from ``{{viz:<uuid>}}`` placeholders, in document order.

    Placeholders inside fenced code blocks are QUOTED examples, not embeds —
    they are skipped. Duplicates are preserved once (first occurrence order).
    """
    spans = _fence_spans(markdown or "")
    seen: List[str] = []
    for m in _VIZ_PLACEHOLDER_RE.finditer(markdown or ""):
        if _in_spans(m.start(), spans):
            continue
        viz_id = m.group(1).lower()
        if viz_id not in seen:
            seen.append(viz_id)
    return seen


class DocEditError(Exception):
    """Raised when a find/replace op cannot be applied. Message is planner-facing."""

    def __init__(self, message: str, op_index: int | None = None):
        super().__init__(message)
        self.op_index = op_index


def apply_find_replace_edits(markdown: str, edits: List[Dict[str, str]]) -> str:
    """Apply find/replace ops ATOMICALLY: validate every op first, then apply.

    Each ``find`` must appear exactly once in the CURRENT document (ops are
    validated against the original text, then applied sequentially — an op may
    not target text produced by an earlier op in the same call).

    Raises DocEditError naming the failing op; on error the document is unchanged.
    """
    if not edits:
        raise DocEditError("No edits provided.")

    current = markdown
    # Validate all ops upfront against the running document state by simulating
    # the application on a working copy. Only if every op succeeds do we return.
    working = current
    for i, op in enumerate(edits):
        find = op.get("find") if isinstance(op, dict) else op.find  # tolerate models
        replace = op.get("replace") if isinstance(op, dict) else op.replace
        if not find:
            raise DocEditError(f"Edit op {i + 1}: `find` must be non-empty.", op_index=i)
        count = working.count(find)
        if count == 0:
            preview = find[:120].replace("\n", "\\n")
            raise DocEditError(
                f"Edit op {i + 1}: `find` text not found in the document: \"{preview}\". "
                f"No edits were applied. Check the current document content and retry with exact text.",
                op_index=i,
            )
        if count > 1:
            preview = find[:120].replace("\n", "\\n")
            raise DocEditError(
                f"Edit op {i + 1}: `find` text matches {count} times: \"{preview}\". "
                f"No edits were applied. Include more surrounding context to make the match unique.",
                op_index=i,
            )
        working = working.replace(find, replace or "", 1)
    return working


def heading_outline(markdown: str, max_items: int = 20) -> List[str]:
    """Return the document's heading outline (e.g. ['# Title', '## Findings'])."""
    spans = _fence_spans(markdown or "")
    outline: List[str] = []
    for m in re.finditer(r"^(#{1,4})\s+(.+)$", markdown or "", re.MULTILINE):
        if _in_spans(m.start(), spans):
            continue
        outline.append(f"{m.group(1)} {m.group(2).strip()}")
        if len(outline) >= max_items:
            break
    return outline
