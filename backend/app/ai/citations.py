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


# Footnote label for a source without a title, per locale.
_FALLBACK_LABELS = {"en": "Result", "es": "Resultado", "he": "תוצאה"}


def fallback_source_label(text: Optional[str], org_locale: Optional[str] = None) -> str:
    """The untitled-source label in the answer's language: its script decides
    (Hebrew answers get Hebrew), otherwise the org locale, then English."""
    from app.services.teams_status_strings import status_locale

    return _FALLBACK_LABELS.get(status_locale(strip_citations(text), org_locale)) or _FALLBACK_LABELS["en"]


def footnote_citations(
    text: Optional[str], sources: Dict[str, str], fallback_label: str = "Result"
) -> Optional[str]:
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
        f"[{number}] {(sources.get(source_id) or '').strip() or fallback_label}"
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


async def _report_org_locale(db, report_id: Optional[str]) -> Optional[str]:
    """The report's org locale, or None."""
    if not report_id:
        return None
    from app.dependencies import _locale_from_org
    from app.models.organization import Organization
    from app.models.report import Report

    org = (await db.execute(
        select(Organization).join(Report, Report.organization_id == Organization.id)
        .where(Report.id == str(report_id))
    )).scalar_one_or_none()
    return _locale_from_org(org) if org is not None else None


async def channel_text(db, report_id: Optional[str], text: Optional[str]) -> Optional[str]:
    """An answer as a chat channel should receive it: citations as footnotes.

    The lookup runs in its own session, never the caller's: a failed query
    must not leave the caller's session in a failed transaction for the rest
    of its send loop. ``db`` is kept for the call signature. Never raises —
    a lookup failure falls back to the text without markers.
    """
    if not text or "{{" not in text or not cited_ids(text):
        return strip_citations(text)
    sources: Dict[str, str] = {}
    org_locale: Optional[str] = None
    try:
        from app.dependencies import async_session_maker

        async with async_session_maker() as own_db:
            sources = await report_citation_sources(own_db, report_id, text)
            try:
                org_locale = await _report_org_locale(own_db, report_id)
            except Exception:
                org_locale = None
    except Exception:
        sources = {}
    return footnote_citations(text, sources, fallback_label=fallback_source_label(text, org_locale))
