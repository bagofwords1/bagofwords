"""edit_note tool — edit a per-report working note in place.

Mirrors edit_doc: surgical atomic find/replace ops (preferred) with a
full-content fallback. Reuses the shared apply_find_replace_edits helper. Unlike
docs, notes are not versioned — the row is updated in place.
"""
import logging
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.edit_note import EditNoteInput, EditNoteOutput
from app.ai.tools.schemas.events import (
    ToolEndEvent,
    ToolEvent,
    ToolProgressEvent,
    ToolStartEvent,
)
from app.models.note import Note

from ._doc_markdown import DocEditError, apply_find_replace_edits
from .create_note import MAX_NOTE_CHARS, note_snapshot

logger = logging.getLogger(__name__)


class EditNoteTool(Tool):
    """Edit an existing per-report note via find/replace ops or full replacement."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="edit_note",
            description=(
                "Edit one of your working notes for this report. PREFER surgical `edits` (find/replace; "
                "each `find` must match the current note exactly once, applied atomically) — ideal for "
                "checking off a todo (find '- [ ] step' → replace '- [x] step') or revising a finding. "
                "For a big rewrite pass full `content` instead. Get the note_id from the <notes> block."
            ),
            category="action",
            version="1.0.0",
            input_schema=EditNoteInput.model_json_schema(),
            output_schema=EditNoteOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=False,
            required_permissions=[],
            is_active=True,
            tags=["note"],
            allowed_modes=None,  # gated by the enable_agent_notes org setting
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return EditNoteInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return EditNoteOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = EditNoteInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"note_id": data.note_id})

        has_edits = bool(data.edits)
        has_content = data.content is not None and data.content.strip() != ""
        if has_edits == has_content:  # neither or both
            yield self._fail(data.note_id, "Provide exactly one of `edits` (find/replace) or `content` (full replacement).")
            return

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        report_id = str(report.id) if report else None

        result = await db.execute(
            select(Note).where(Note.id == data.note_id, Note.deleted_at.is_(None))
        )
        note = result.scalar_one_or_none()
        if note is None:
            yield self._fail(data.note_id, f"Note {data.note_id} not found.")
            return
        if report_id and str(note.report_id) != report_id:
            yield self._fail(data.note_id, f"Note {data.note_id} does not belong to this report.")
            return

        diff_applied = False
        if has_edits:
            try:
                ops = [op.model_dump() for op in (data.edits or [])]
                new_content = apply_find_replace_edits(note.content or "", ops)
                diff_applied = True
            except DocEditError as e:
                yield self._fail(data.note_id, str(e), error_type="edit_match_error")
                return
        else:
            new_content = data.content or ""

        if not new_content.strip():
            yield self._fail(data.note_id, "The edit would leave the note empty.")
            return
        if len(new_content) > MAX_NOTE_CHARS:
            yield self._fail(data.note_id, f"Note would be too long ({len(new_content)} chars; max {MAX_NOTE_CHARS}).")
            return

        note.content = new_content
        if data.title is not None:
            note.title = data.title
        await db.commit()
        await db.refresh(note)

        yield ToolProgressEvent(
            type="tool.progress",
            payload={"stage": "note_edited", "note_id": str(note.id), "timing": False},
        )

        output = {"success": True, "note_id": str(note.id), "title": note.title, "diff_applied": diff_applied}
        observation = {
            "summary": (
                f"Edited note{f' \"{note.title}\"' if note.title else ''} (note_id: {note.id}) "
                f"via {'surgical edits' if diff_applied else 'full replacement'}."
            ),
            "note_id": str(note.id),
            "title": note.title,
            "diff_applied": diff_applied,
            "content_snapshot": note_snapshot(new_content),
        }
        yield ToolEndEvent(type="tool.end", payload={"output": output, "observation": observation})

    def _fail(self, note_id: str, message: str, error_type: str = "validation_error") -> ToolEndEvent:
        return ToolEndEvent(
            type="tool.end",
            payload={
                "output": {"success": False, "note_id": note_id, "error": message},
                "observation": {
                    "summary": f"Failed to edit note: {message}",
                    "note_id": note_id,
                    "error": {"type": error_type, "message": message},
                },
            },
        )
