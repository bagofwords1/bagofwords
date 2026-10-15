"""finish_subtask — the mandatory terminal tool of a sub-agent (mode=subagent).

Validates the typed record, stores it on the sub_agents row and ends the child
run (analysis_complete). The runtime adds the envelope at finalize.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, ValidationError

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import ToolEndEvent, ToolErrorEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.sub_agents import FinishSubtaskInput

logger = logging.getLogger(__name__)


class FinishSubtaskTool(Tool):

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="finish_subtask",
            description=(
                "ACTION: End this sub-agent with its typed record — the ONLY way your work reaches the "
                "parent (your final chat text is not delivered). Call it as soon as the goal is settled, "
                "and always before your step budget runs out. outcome: for a hypothesis supported | "
                "refuted | inconclusive; otherwise done | partial | blocked. Put the one number that "
                "carries the answer in `summary`; list every id a reader can verify in `evidence` "
                "(steps/queries/vizs you created, notes, files, tables); name what you checked and "
                "excluded in `ruled_out`; data gaps and assumed windows in `caveats`; if inconclusive or "
                "blocked, the single thing that would settle it in `unknown`. If you kept a note, pass "
                "its id as report_note_id."
            ),
            category="action",
            version="1.0.0",
            input_schema=FinishSubtaskInput.model_json_schema(),
            output_schema={"type": "object", "properties": {"status": {"type": "string"}}},
            max_retries=0,
            timeout_seconds=15,
            idempotent=True,
            required_permissions=[],
            allowed_modes=["subagent"],
            observation_policy="always",
            tags=["sub-agent", "finish", "record"],
        )

    @property
    def input_model(self) -> type[BaseModel]:
        return FinishSubtaskInput

    async def run_stream(self, tool_input: dict[str, Any], runtime_ctx: dict[str, Any]) -> AsyncIterator[ToolEvent]:
        try:
            data = FinishSubtaskInput(**(tool_input or {}))
        except ValidationError as e:
            # The error IS the next observation: the child corrects and calls again.
            yield ToolErrorEvent(type="tool.error", payload={
                "message": f"finish_subtask rejected — fix these fields and call it again: {e}",
                "code": "INVALID_INPUT",
            })
            return
        yield ToolStartEvent(type="tool.start", payload={"outcome": data.outcome})

        head = runtime_ctx.get("head_completion")
        sub_agent_id = None
        try:
            sub_agent_id = (head.prompt or {}).get("sub_agent_id") if head is not None and isinstance(head.prompt, dict) else None
        except Exception:
            sub_agent_id = None
        sub_agent_id = sub_agent_id or runtime_ctx.get("sub_agent_id")
        if not sub_agent_id:
            yield ToolErrorEvent(type="tool.error", payload={"message": "Not running as a sub-agent.", "code": "NOT_A_SUB_AGENT"})
            return

        from app.dependencies import async_session_maker
        from app.services.sub_agent_service import PendingSubAgentMessages, sub_agent_service
        record = data.model_dump()
        try:
            # A rejected finish rolls back its mailbox locks. Keep that
            # transaction separate from the runner's pending tool/block writes.
            async with (runtime_ctx.get("session_maker") or async_session_maker)() as db:
                row = await sub_agent_service.record_finish(db, sub_agent_id=str(sub_agent_id), result=record,
                    can_review_messages=runtime_ctx.get("sub_agent_can_review_messages", True))
        except PendingSubAgentMessages as e:
            yield ToolErrorEvent(type="tool.error", payload={"message": str(e), "code": "PENDING_AGENT_MESSAGES"})
            return
        except ValueError as e:
            yield ToolErrorEvent(type="tool.error", payload={"message": str(e), "code": "SUB_AGENT_ENDED"})
            return
        except Exception as e:
            logger.exception("finish_subtask: could not store the record")
            yield ToolErrorEvent(type="tool.error", payload={"message": f"Could not store the record: {e}", "code": "SUB_AGENT_ERROR"})
            return
        if row is None:
            yield ToolErrorEvent(type="tool.error", payload={"message": "Sub-agent row not found.", "code": "SUB_AGENT_ERROR"})
            return
        record = row.result_json or record
        runtime_ctx["sub_agent_finished"] = True
        yield ToolEndEvent(type="tool.end", payload={
            "output": {"status": "recorded", "sub_agent_id": str(row.id), "outcome": record["outcome"],
                       "caveats": record.get("caveats", [])},
            "observation": {
                "summary": f"Record stored ({record['outcome']}, confidence {record['confidence']}). Sub-agent finished.",
                "success": True,
                "artifacts": [],
                "analysis_complete": True,
                "final_answer": record["summary"],
                "outcome": record["outcome"],
            },
        })
