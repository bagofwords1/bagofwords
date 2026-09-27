"""edit_memory — update (supersede) or delete (forget) one memory entry."""
import logging
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.implementations._memory_common import (
    BOUNDARY, evidence_for, fail, gate, memory_session, record, user_message,
)
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.memory import EditMemoryInput, EditMemoryOutput
from app.services.memory_rules import MemoryValidationError, appends_new_fact, is_direct_edit_request
from app.services.memory_service import memory_service

logger = logging.getLogger(__name__)

# tool field -> service field
_FIELDS = {"text": "text", "tags": "tags", "aliases": "aliases", "date": "event_start",
           "end_date": "event_end", "expires_at": "expires_at"}


class EditMemoryTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="edit_memory",
            description=(
                "Update or delete one fact in the current user's memory by its handle (e.g. \"m7\" from "
                "<memory> or search_memory). Use update when THAT fact changed or to merge a near-duplicate "
                "of it — never to append a different fact (that is a new create_memory); use delete when the "
                "user asks you to "
                "forget something or it is no longer true. " + BOUNDARY + " Entries the user wrote "
                "themselves can only be changed when the user directly asks for that change in this "
                "message; otherwise point them to their profile (Profile → Memory)."
            ),
            category="action",
            version="1.0.0",
            input_schema=EditMemoryInput.model_json_schema(),
            output_schema=EditMemoryOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=30,
            idempotent=False,
            required_permissions=[],
            is_active=True,
            tags=["memory"],
            allowed_modes=["chat"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return EditMemoryInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return EditMemoryOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = EditMemoryInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": data.title or "Updating memory"})

        blocked = gate(runtime_ctx)
        if blocked:
            yield fail("Memory not changed", blocked, code="unavailable")
            return

        org = runtime_ctx["organization"]
        user = runtime_ctx["user"]
        changes = {svc: getattr(data, k) for k, svc in _FIELDS.items() if getattr(data, k) is not None}
        if data.action == "update" and not changes:
            yield fail("Memory not changed", "Nothing to update — pass the fields to change.")
            return
        try:
            async with memory_session(runtime_ctx) as db:
                entry = await memory_service.resolve_handle(db, str(org.id), str(user.id), data.handle)
                if entry is None:
                    yield fail(
                        "Memory not changed",
                        f"Unknown memory handle '{data.handle}'. Use a handle shown in <memory> or returned by "
                        "search_memory.",
                        code="not_found",
                    )
                    return
                if entry.source == "user" and not is_direct_edit_request(
                    user_message(runtime_ctx), entry.text, entry.tags or []
                ):
                    record(runtime_ctx, "refusals", {
                        "tool": "edit_memory", "code": "memory.user_authored", "handle": entry.handle,
                        "text": (data.text or "")[:400],
                    })
                    yield fail(
                        "Memory not changed",
                        f"[{entry.handle}] was written by the user themselves. Only change it when the user "
                        "directly asks for that change in their message; otherwise tell them they can edit "
                        "it in their profile (Profile → Memory).",
                        code="memory.user_authored",
                    )
                    return
                old_handle = entry.handle
                if data.action == "update" and data.text and appends_new_fact(entry.text, data.text):
                    record(runtime_ctx, "refusals", {
                        "tool": "edit_memory", "code": "memory.one_fact_per_entry", "handle": entry.handle,
                        "text": data.text[:400],
                    })
                    yield fail(
                        "Memory not changed",
                        f"[{entry.handle}] already says that; the extra part is a different fact. Keep "
                        f"[{entry.handle}] as it is and save the new fact with create_memory (one fact per entry).",
                        code="memory.one_fact_per_entry",
                    )
                    return
                if data.action == "delete":
                    await memory_service.forget(db, entry)
                    new_handle, summary = old_handle, f"Forgot [{old_handle}]."
                    new_id = str(entry.id)
                else:
                    new = await memory_service.update(
                        db, entry, changes=changes, source="agent",
                        evidence=evidence_for(runtime_ctx, changes.get("text") or entry.text),
                    )
                    new_handle, new_id = new.handle, str(new.id)
                    summary = f"Updated [{old_handle}] → now [{new_handle}]."
        except MemoryValidationError as e:
            record(runtime_ctx, "refusals", {
                "tool": "edit_memory", "code": e.code, "handle": data.handle, "text": (data.text or "")[:400],
            })
            yield fail("Memory not changed", e.message, code=e.code)
            return
        except Exception as e:  # pragma: no cover
            logger.exception("edit_memory failed")
            yield fail("Memory not changed", f"Unexpected error: {e}", code="internal")
            return

        record(runtime_ctx, "writes", {
            "tool": "edit_memory", "action": data.action, "id": new_id, "handle": new_handle,
            "previous_handle": old_handle,
        })
        output = {"success": True, "handle": new_handle}
        observation = {"summary": summary, "handle": new_handle, "previous_handle": old_handle, "action": data.action}
        yield ToolEndEvent(type="tool.end", payload={"output": output, "observation": observation})
