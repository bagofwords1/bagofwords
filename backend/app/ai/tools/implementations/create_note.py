"""create_note tool — write a per-report working note (the agent's scratchpad).

The planner authors the note content directly in the tool args; this tool only
validates and persists it. Notes are injected back into the planner (and the
knowledge harness) every iteration and shown in the report UI.
"""
import logging
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.create_note import CreateNoteInput, CreateNoteOutput
from app.ai.tools.schemas.events import (
    ToolEndEvent,
    ToolEvent,
    ToolProgressEvent,
    ToolStartEvent,
)
from app.models.note import Note

logger = logging.getLogger(__name__)

MAX_NOTE_CHARS = 20_000
_SNAPSHOT_LIMIT = 2000


def note_snapshot(content: str) -> str:
    if len(content) <= _SNAPSHOT_LIMIT:
        return content
    return content[:_SNAPSHOT_LIMIT] + "\n...[note truncated]"


class CreateNoteTool(Tool):
    """Create a per-report working note (agent scratchpad memory)."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_note",
            description=(
                "Write a working note for THIS report — your own scratchpad memory. Use it to keep a "
                "plan (`- [ ]` task lists), record findings (cite the table/column/viz), note ruled-out "
                "hypotheses, or pin down definitions. Notes are shown back to you every step (and to the "
                "user), so create one early and keep it current with edit_note. Notes are your memory, "
                "not user instructions or a deliverable — for a written report use create_doc instead."
            ),
            category="action",
            version="1.0.0",
            input_schema=CreateNoteInput.model_json_schema(),
            output_schema=CreateNoteOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=False,
            required_permissions=[],
            is_active=True,
            tags=["note"],
            allowed_modes=None,  # gated by the enable_agent_notes org setting, not by mode
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreateNoteInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreateNoteOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = CreateNoteInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": data.title or "Note"})

        content = data.content or ""
        if not content.strip():
            yield self._fail("Note content is empty.")
            return
        if len(content) > MAX_NOTE_CHARS:
            yield self._fail(f"Note is too long ({len(content)} chars; max {MAX_NOTE_CHARS}).")
            return

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        user = runtime_ctx.get("user")
        organization = runtime_ctx.get("organization")
        agent_execution = runtime_ctx.get("agent_execution") or runtime_ctx.get("current_execution")

        note = Note(
            report_id=str(report.id) if report else None,
            organization_id=str(organization.id) if organization else None,
            agent_execution_id=str(agent_execution.id) if agent_execution else None,
            user_id=str(user.id) if user else None,
            title=data.title,
            content=content,
            source="agent",
        )
        db.add(note)
        await db.commit()
        await db.refresh(note)

        yield ToolProgressEvent(
            type="tool.progress",
            payload={"stage": "note_created", "note_id": str(note.id), "timing": False},
        )

        output = {"success": True, "note_id": str(note.id), "title": note.title}
        observation = {
            "summary": f"Created note{f' \"{note.title}\"' if note.title else ''} (note_id: {note.id}).",
            "note_id": str(note.id),
            "title": note.title,
            "content_snapshot": note_snapshot(content),
        }
        yield ToolEndEvent(type="tool.end", payload={"output": output, "observation": observation})

    def _fail(self, message: str) -> ToolEndEvent:
        return ToolEndEvent(
            type="tool.end",
            payload={
                "output": {"success": False, "error": message},
                "observation": {
                    "summary": f"Failed to create note: {message}",
                    "error": {"type": "validation_error", "message": message},
                },
            },
        )
