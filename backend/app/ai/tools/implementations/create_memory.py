"""create_memory — save one durable, personal fact about the current user.

Replaces the full-document ``update_user_memory`` rewrite: each fact is its
own row, so parallel sessions can't clobber each other and nothing is lost
when the model prunes. Validation, dedupe, the definition/rule heuristic and
the secret filter all live in MemoryService.
"""
import logging
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.implementations._memory_common import (
    BOUNDARY, evidence_for, fail, gate, memory_session, record,
)
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.memory import CreateMemoryInput, CreateMemoryOutput
from app.services.memory_rules import MemoryValidationError
from app.services.memory_service import memory_service

logger = logging.getLogger(__name__)


class CreateMemoryTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_memory",
            description=(
                "Save a personal fact about the current user: their style, role, schedule, focus, or "
                "their own shorthand. NOT for business definitions, metric logic or rules; those belong "
                "in instructions (create_instruction). " + BOUNDARY + " Save when you NOTICE something "
                "durable (a correction of your format, a stated role, their shorthand, a dated meeting "
                "or time off, what they work on now) — not only when asked. One declarative fact per "
                "call (\"Prefers…\", never \"Always…\"). If <memory> already has a matching entry, use "
                "edit_memory instead. Never store one-off task details, data values, facts about other "
                "people, secrets or health details."
            ),
            category="action",
            version="1.0.0",
            input_schema=CreateMemoryInput.model_json_schema(),
            output_schema=CreateMemoryOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=30,
            idempotent=False,
            required_permissions=[],
            is_active=True,
            tags=["memory"],
            # Human-initiated chat turns only (every channel). Never training
            # or knowledge runs; machine turns are removed from the catalog.
            allowed_modes=["chat"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreateMemoryInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreateMemoryOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = CreateMemoryInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": data.title or "Updating memory"})

        blocked = gate(runtime_ctx)
        if blocked:
            yield fail("Memory not saved", blocked, code="unavailable")
            return

        org = runtime_ctx["organization"]
        user = runtime_ctx["user"]
        try:
            async with memory_session(runtime_ctx) as db:
                result = await memory_service.create(
                    db,
                    organization_id=str(org.id),
                    user_id=str(user.id),
                    text=data.text,
                    section=data.section,
                    tags=data.tags,
                    aliases=data.aliases,
                    event_start=data.event_start,
                    event_end=data.event_end,
                    expires_at=data.expires_at,
                    source="agent",
                    evidence=evidence_for(runtime_ctx, data.text),
                )
                entry = result.entry
                handle, text, section = entry.handle, entry.text, entry.section
                entry_id = str(entry.id)
        except MemoryValidationError as e:
            record(runtime_ctx, "refusals", {
                "tool": "create_memory", "code": e.code, "section": data.section, "text": data.text[:400],
            })
            yield fail("Memory not saved", e.message, code=e.code)
            return
        except Exception as e:  # pragma: no cover - unexpected DB failure
            logger.exception("create_memory failed")
            yield fail("Memory not saved", f"Unexpected error: {e}", code="internal")
            return

        record(runtime_ctx, "writes", {
            "tool": "create_memory", "id": entry_id, "handle": handle, "section": section,
            "deduped": result.deduped,
        })
        if result.deduped:
            summary = f"Already remembered as [{handle}] — strengthened it instead of adding a duplicate."
            output = {"success": True, "handle": handle, "deduped_into": handle}
        else:
            summary = f"Saved to memory as [{handle}] ({section})."
            output = {"success": True, "handle": handle}
        observation = {"summary": summary, "handle": handle, "section": section, "text": text}
        if result.deduped:
            observation["deduped_into"] = handle
        if result.evicted:
            observation["evicted"] = result.evicted
        yield ToolEndEvent(type="tool.end", payload={"output": output, "observation": observation})
