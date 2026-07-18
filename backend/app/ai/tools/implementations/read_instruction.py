"""Read Instruction Tool — pull the full text of a single instruction or skill on demand.

Instructions and skills are advertised in the prompt (<available_skills> and
<available_instructions>) as a compact catalog of short id + title + description,
with their full text withheld. When the agent decides an entry is relevant to
the user's request, it calls this tool with the short id prefix to load the
full text.

Design constraints (chat-only progressive disclosure):
  - Available in CHAT mode only (allowed_modes=["chat"]).
  - Resolves any published instruction (kind='instruction' or 'skill').
  - Accepts the SHORT id prefix (first part of the UUID), not just the full id.
  - HARD-SCOPED to the current report's agents: the id is verified against the
    report's data sources (or global instructions) and per-user table
    accessibility before any text is returned. When no report scope can be
    resolved, the tool refuses instead of falling back to org-wide reads.
  - Records an 'on_demand' usage event so agent-pulled instructions climb the
    usage-based ranking of the fill/catalog ordering.
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
    """Read the full text of one instruction or skill by its short id prefix."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_instruction",
            description=(
                "RESEARCH: Read the full text of a single instruction or skill by "
                "the SHORT id prefix shown in <available_skills> or "
                "<available_instructions>. Entries are listed there with only a "
                "title + short description — their full content is withheld to keep "
                "the prompt small. When a listed entry is relevant to the user's "
                "request, call this with its short id (e.g. 'be8090f2') to load the "
                "full text BEFORE acting on it. Only instructions for this report's "
                "connected data (or global ones) are readable."
            ),
            category="research",
            version="2.0.0",
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
                    "description": (
                        "Read an instruction/skill by its short id prefix from "
                        "<available_skills> or <available_instructions>"
                    ),
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
            # Guard: without a report context this tool does NOT fall back to
            # org-wide reads — the readable set must equal what could have been
            # advertised to THIS report's agents.
            scope_resolved, data_source_ids = self._resolve_scope(runtime_ctx)
            if not scope_resolved:
                yield self._end_error(
                    "read_instruction is only available within a report session."
                )
                return

            # --- Step 1: resolve the short id prefix to a unique instruction ---
            # Case-insensitive prefix match over published, non-deleted rows of
            # any kind (instruction or skill).
            stmt = (
                select(
                    Instruction.id,
                    Instruction.title,
                    Instruction.description,
                    Instruction.kind,
                )
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
            candidates: List[Tuple[str, Optional[str], Optional[str], Optional[str]]] = [
                (str(row[0]), row[1], row[2], row[3]) for row in result.all()
            ]

            if not candidates:
                yield self._end_error(
                    f"No instruction found with id starting '{prefix}'. "
                    f"Use a short id exactly as shown in <available_skills> or "
                    f"<available_instructions>."
                )
                return

            if len(candidates) > 1:
                listing = ", ".join(
                    f"{cid[:8]} ({title or 'untitled'})" for cid, title, _d, _k in candidates[:10]
                )
                yield self._end_error(
                    f"Ambiguous id prefix '{prefix}' matched {len(candidates)} entries: "
                    f"{listing}. Pass a longer prefix to disambiguate."
                )
                return

            full_id, title, description, kind = candidates[0]

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
            await self._record_on_demand_usage(runtime_ctx, item, full_id)

            output = ReadInstructionOutput(
                success=True,
                id=full_id,
                short_id=full_id[:8],
                title=item.title or title,
                description=description,
                text=item.text or "",
                category=item.category,
                kind=kind or "instruction",
                load_mode=item.load_mode,
                message=f"Read instruction {full_id[:8]}",
            )

            summary = f"Read instruction '{item.title or title or full_id[:8]}'"
            output_dict = output.model_dump()
            # Surface the loaded instruction to the UI (instructions.context SSE
            # + completion hydration) via the shared related_instructions hook.
            output_dict["related_instructions"] = [
                {
                    "id": full_id,
                    "title": item.title or title,
                    "category": item.category,
                    "load_mode": item.load_mode,
                    "source_type": "on_demand",
                }
            ]
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output_dict,
                    "observation": {
                        "summary": summary,
                        "artifacts": [
                            {
                                "type": "instruction_read_result",
                                "id": full_id,
                                "title": item.title or title,
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
    async def _record_on_demand_usage(runtime_ctx: Dict[str, Any], item, full_id: str) -> None:
        """Record an 'on_demand' usage event (best-effort) so pulled
        instructions climb the usage-based catalog/fill ordering."""
        try:
            from app.services.instruction_usage_service import InstructionUsageService
            from app.schemas.instruction_usage_schema import InstructionUsageEventCreate

            db = runtime_ctx.get("db")
            organization = runtime_ctx.get("organization")
            user = runtime_ctx.get("user")
            report = runtime_ctx.get("report")
            await InstructionUsageService().record_usage_event(
                db,
                InstructionUsageEventCreate(
                    org_id=str(organization.id),
                    report_id=str(report.id) if report is not None else None,
                    instruction_id=full_id,
                    user_id=str(user.id) if user is not None else None,
                    load_mode=item.load_mode or "intelligent",
                    load_reason="on_demand",
                    source_type=item.source_type,
                    category=item.category,
                    title=item.title,
                ),
            )
        except Exception:
            logger.debug("read_instruction: usage event failed", exc_info=True)

    @staticmethod
    def _resolve_scope(runtime_ctx: Dict[str, Any]) -> Tuple[bool, Optional[List[str]]]:
        """Derive the report's data-source scope for per-id verification.

        Returns (resolved, data_source_ids):
        - resolved=False → no report context at all; the caller REFUSES the
          read (no org-wide fallback).
        - resolved=True, ds_ids=list → readable = instructions on those data
          sources or global ones (mirrors what the catalog advertised).
        - resolved=True, ds_ids=None → the report/hub itself is unscoped (its
          catalog advertised org-wide), so reads mirror that.

        Prefer the context hub's instruction builder (already constructed with
        the report's data sources); fall back to the report's own data sources.
        """
        context_hub = runtime_ctx.get("context_hub")
        if context_hub is not None:
            ib = getattr(context_hub, "instruction_builder", None)
            if ib is not None:
                return True, getattr(ib, "data_source_ids", None)
            data_sources = getattr(context_hub, "data_sources", None)
            if data_sources is not None:
                return True, [str(ds.id) for ds in data_sources]

        report = runtime_ctx.get("report")
        if report is not None:
            try:
                return True, [str(ds.id) for ds in (report.data_sources or [])]
            except Exception:
                # Data sources not loadable — default to global-only, never org-wide.
                return True, []
        return False, None

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
