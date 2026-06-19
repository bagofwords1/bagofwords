"""Read Instruction Tool — pull the full text of a single instruction/skill on demand.

Skills are advertised in the prompt (<available_skills>) as a compact catalog of
short id + title + description, with their full text withheld. When the agent
decides a skill is relevant to the user's request, it calls this tool with the
short id prefix to load the full text.

Design constraints (chat-only progressive disclosure):
  - Available in CHAT mode only (allowed_modes=["chat"]).
  - Accepts the SHORT id prefix (first part of the UUID), not just the full id.
  - Scoped to the current report's agents — only instructions for the attached
    data sources (or global/blank instructions) are readable, and per-user table
    accessibility still applies.
"""

from typing import AsyncIterator, Dict, Any, Type, List, Optional, Tuple
from pydantic import BaseModel
import logging

from sqlalchemy import select, and_, func

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.read_instruction import (
    ReadInstructionInput,
    ReadInstructionOutput,
)
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)
from app.models.instruction import Instruction
from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

logger = logging.getLogger(__name__)


class ReadInstructionTool(Tool):
    """Read the full text of one instruction/skill by its short id prefix."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_instruction",
            description=(
                "RESEARCH: Read the full text of a single skill/instruction by the "
                "SHORT id prefix shown in <available_skills>. Skills are listed there "
                "with only a title + short description — their full content is withheld "
                "to keep the prompt small. When a listed skill is relevant to the user's "
                "request, call this with its short id (e.g. 'be8090f2') to load the full "
                "instructions BEFORE acting on them. Only skills/instructions for this "
                "report's connected data (or global ones) are readable."
            ),
            category="research",
            version="1.0.0",
            input_schema=ReadInstructionInput.model_json_schema(),
            output_schema=ReadInstructionOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=15,
            idempotent=True,
            required_permissions=[],
            tags=["instruction", "skill", "read", "knowledge"],
            allowed_modes=["chat"],
            examples=[
                {
                    "input": {"id": "be8090f2"},
                    "description": "Read a skill by its short id prefix from <available_skills>",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ReadInstructionInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ReadInstructionOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = ReadInstructionInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        prefix = (data.id or "").strip().lower()
        yield ToolStartEvent(type="tool.start", payload={"id": prefix})

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")

        if not all([db, organization]):
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": "Missing required runtime context (db, organization)",
                    "code": "MISSING_CONTEXT",
                },
            )
            return

        try:
            # --- Resolve the scope: this report's data sources (+ global) ---
            data_source_ids = self._resolve_data_source_ids(runtime_ctx)

            # --- Step 1: resolve the short id prefix to a unique instruction ---
            # Case-insensitive prefix match over published, non-deleted org rows.
            stmt = (
                select(Instruction.id, Instruction.kind, Instruction.title)
                .where(
                    and_(
                        Instruction.organization_id == organization.id,
                        Instruction.status == "published",
                        Instruction.deleted_at.is_(None),
                        func.lower(Instruction.id).like(prefix + "%"),
                    )
                )
            )
            result = await db.execute(stmt)
            candidates: List[Tuple[str, Optional[str], Optional[str]]] = [
                (str(row[0]), row[1], row[2]) for row in result.all()
            ]

            if not candidates:
                yield self._end_error(
                    f"No instruction found with id starting '{prefix}'. "
                    f"Use a short id exactly as shown in <available_skills>."
                )
                return

            if len(candidates) > 1:
                listing = ", ".join(
                    f"{cid[:8]} ({title or 'untitled'})" for cid, _kind, title in candidates[:10]
                )
                yield self._end_error(
                    f"Ambiguous id prefix '{prefix}' matched {len(candidates)} instructions: "
                    f"{listing}. Pass a longer prefix to disambiguate."
                )
                return

            full_id, kind, _title = candidates[0]

            # --- Step 2: load the full (versioned) text, scoped to this report's
            # data sources, with per-user table accessibility applied. ---
            builder = InstructionContextBuilder(
                db,
                organization,
                current_user=user,
                data_source_ids=data_source_ids,
            )
            items = await builder.load_instructions_by_ids([full_id], load_mode_filter=None)

            if not items:
                yield self._end_error(
                    f"Instruction {full_id[:8]} exists but is not available for this "
                    f"report's connected data (or you don't have access to its tables)."
                )
                return

            item = items[0]
            output = ReadInstructionOutput(
                success=True,
                id=full_id,
                short_id=full_id[:8],
                title=item.title,
                text=item.text or "",
                category=item.category,
                kind=kind or "instruction",
                load_mode=item.load_mode,
                message=f"Read instruction {full_id[:8]}",
            )

            summary = f"Read {kind or 'instruction'} '{item.title or full_id[:8]}'"
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": summary,
                        "artifacts": [
                            {
                                "type": "instruction_read_result",
                                "id": full_id,
                                "title": item.title,
                                "kind": kind or "instruction",
                            }
                        ],
                    },
                },
            )
        except Exception as e:
            logger.exception(f"read_instruction failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Read failed: {e}", "code": "READ_FAILED"},
            )

    @staticmethod
    def _resolve_data_source_ids(runtime_ctx: Dict[str, Any]) -> Optional[List[str]]:
        """Derive the report's data source ids for scoping.

        Prefer the context hub's instruction builder (already constructed with the
        report's data sources). Fall back to the report's own data sources. Returns
        None only when no scope can be determined (caller then sees org-wide rows,
        still gated by published/accessibility).
        """
        context_hub = runtime_ctx.get("context_hub")
        if context_hub is not None:
            ib = getattr(context_hub, "instruction_builder", None)
            if ib is not None and getattr(ib, "data_source_ids", None) is not None:
                return ib.data_source_ids
            data_sources = getattr(context_hub, "data_sources", None)
            if data_sources:
                return [str(ds.id) for ds in data_sources]

        report = runtime_ctx.get("report")
        if report is not None:
            try:
                if report.data_sources:
                    return [str(ds.id) for ds in report.data_sources]
            except Exception:
                pass
        return None

    @staticmethod
    def _end_error(message: str) -> ToolEndEvent:
        """A 'soft' failure surfaced as a normal observation so the agent can
        adjust (pass a longer/correct id) rather than treating it as a hard error."""
        output = ReadInstructionOutput(success=False, message=message)
        return ToolEndEvent(
            type="tool.end",
            payload={
                "output": output.model_dump(),
                "observation": {"summary": message, "artifacts": []},
            },
        )
