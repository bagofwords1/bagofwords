"""Render a report's agent notes into the ``<notes>`` context block.

Notes change during a run (create_note/edit_note), so this is queried fresh on
each planner iteration rather than cached in the static context view.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.models.note import Note

# Keep the injected block bounded — collapse very long notes to a snippet.
_PER_NOTE_LIMIT = 4000
_MAX_NOTES = 40


def _clip(text: str) -> str:
    text = text or ""
    if len(text) <= _PER_NOTE_LIMIT:
        return text
    return text[:_PER_NOTE_LIMIT] + "\n...[note truncated — full text via the report notes]"


async def build_notes_context(db: Any, report_id: str) -> str:
    """Return a ``<notes>…</notes>`` string for the report, or "" if there are none.

    Each note renders as ``<note id="…" title="…">content</note>`` so the planner
    can reference it by id in edit_note.
    """
    if not report_id:
        return ""
    try:
        result = await db.execute(
            select(Note)
            .where(Note.report_id == str(report_id), Note.deleted_at.is_(None))
            .order_by(Note.created_at.asc())
            .limit(_MAX_NOTES)
        )
        notes = list(result.scalars().all())
    except Exception:
        return ""
    if not notes:
        return ""

    lines = ["<notes>"]
    for n in notes:
        title = (n.title or "").replace('"', "'")
        lines.append(f'  <note id="{n.id}" title="{title}">')
        lines.append(_clip(n.content))
        lines.append("  </note>")
    lines.append("</notes>")
    return "\n".join(lines)
