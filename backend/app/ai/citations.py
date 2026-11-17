"""Citation markers in an answer: ``{{cite:<visualization id>}}``.

The planner marks a finding with the result it came from by appending
``{{cite:<id>}}`` right after the sentence, where ``<id>`` is a visualization
id from an earlier ``create_data`` result (the first 8 characters are enough).

The marker is stored in the answer as written. What a reader sees depends on
where the answer is shown:

* the web report and the shared link turn it into a numbered chip that
  previews and opens the cited result (``frontend/composables/useCitations.ts``
  mirrors the grammar here);
* a chat channel has no chip to render, so ``footnote_citations`` rewrites the
  markers to ``[1]``, ``[2]`` … and appends one line per source;
* anywhere else the text leaves the product as plain text, ``strip_citations``
  removes them.

A marker that names nothing in the report is dropped, never shown: a made-up
citation must not read as a source.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select

# Shortest id prefix accepted: the first UUID group.
MIN_ID_CHARS = 8

# The marker, with the horizontal whitespace before it: removing a marker must
# not leave "revenue grew ." behind.
CITATION_RE = re.compile(r"[ \t]*\{\{\s*cite\s*:\s*([0-9A-Za-z-]{1,64})\s*\}\}")
_CODE_FENCE_RE = re.compile(r"(```.*?```)", re.DOTALL)


def _key(raw: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (raw or "").lower())


def _outside_code(text: str, convert) -> str:
    """Apply ``convert`` to prose only: a fenced block is quoted, not cited."""
    parts = _CODE_FENCE_RE.split(text)
    return "".join(p if i % 2 else convert(p) for i, p in enumerate(parts))


def cited_ids(text: Optional[str]) -> List[str]:
    """Ids as written in ``text``'s markers, in order, without repeats."""
    seen: Dict[str, None] = {}
    for part_index, part in enumerate(_CODE_FENCE_RE.split(text or "")):
        if part_index % 2:
            continue
        for match in CITATION_RE.finditer(part):
            seen.setdefault(match.group(1), None)
    return list(seen)


def strip_citations(text: Optional[str]) -> Optional[str]:
    """``text`` with every citation marker removed."""
    if not text or "{{" not in text:
        return text
    return _outside_code(text, lambda prose: CITATION_RE.sub("", prose))


def match_citation(raw: str, candidates: Iterable[str]) -> Optional[str]:
    """The one candidate id ``raw`` names — in full or by its leading
    characters — or None when it names none, or more than one."""
    key = _key(raw)
    if len(key) < MIN_ID_CHARS:
        return None
    hits = [c for c in dict.fromkeys(str(c) for c in candidates if c) if _key(c).startswith(key)]
    return hits[0] if len(hits) == 1 else None


def footnote_citations(text: Optional[str], sources: Dict[str, str]) -> Optional[str]:
    """Rewrite markers for a surface that renders plain text.

    ``sources`` maps a visualization id to its title. Each cited source gets a
    number in order of first mention; its markers become ``[n]`` and one
    ``[n] Title`` line per source is appended. Markers that match no source
    are removed. Without any valid citation the text is returned bare.
    """
    if not text or "{{" not in text:
        return text
    numbers: Dict[str, int] = {}

    def replace(match: "re.Match[str]") -> str:
        source_id = match_citation(match.group(1), sources.keys())
        if not source_id:
            return ""
        number = numbers.setdefault(source_id, len(numbers) + 1)
        return f" [{number}]"

    body = _outside_code(text, lambda prose: CITATION_RE.sub(replace, prose))
    if not numbers:
        return body
    lines = [
        f"[{number}] {(sources.get(source_id) or '').strip() or 'Result'}"
        for source_id, number in numbers.items()
    ]
    return body.rstrip() + "\n\n" + "\n".join(lines)


async def report_citation_sources(db, report_id: Optional[str], text: Optional[str]) -> Dict[str, str]:
    """The ``{visualization id: title}`` a text's markers can resolve against:
    the visualizations of this report only, so a citation never reaches into
    another report. Empty (and no query) when the text cites nothing."""
    if not report_id or not cited_ids(text):
        return {}
    from app.models.visualization import Visualization

    rows = (await db.execute(
        select(Visualization.id, Visualization.title).where(Visualization.report_id == str(report_id))
    )).all()
    return {str(row[0]): (row[1] or "") for row in rows}


async def channel_text(db, report_id: Optional[str], text: Optional[str]) -> Optional[str]:
    """An answer as a chat channel should receive it: citations as footnotes.
    Never raises — a lookup failure falls back to the text without markers."""
    if not text or "{{" not in text:
        return text
    try:
        sources = await report_citation_sources(db, report_id, text)
    except Exception:
        sources = {}
    return footnote_citations(text, sources)
