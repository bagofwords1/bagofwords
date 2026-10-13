"""stop_sub_agent — stop one running sub-agent (the wave continues)."""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel, ValidationError

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import ToolEndEvent, ToolErrorEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.sub_agents import StopSubAgentInput

logger = logging.getLogger(__name__)


class StopSubAgentTool(Tool):

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="stop_sub_agent",
            description=(
                "ACTION: Stop one running sub-agent by id when the user asks ('stop the pricing one') or its "
                "goal became moot. It is recorded as interrupted with a partial record; the rest of its "
                "wave keeps running and still reports back."
            ),
            category="action",
            version="1.0.0",
            input_schema=StopSubAgentInput.model_json_schema(),
            output_schema={"type": "object"},
            max_retries=0,
            timeout_seconds=20,
            idempotent=True,
            required_permissions=[],
            allowed_modes=["chat", "training"],
            observation_policy="always",
            tags=["sub-agent", "stop"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return StopSubAgentInput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        try:
            data = StopSubAgentInput(**(tool_input or {}))
        except ValidationError as e:
            yield ToolErrorEvent(type="tool.error", payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"})
            return
        yield ToolStartEvent(type="tool.start", payload={"sub_agent_id": data.sub_agent_id})
        from app.services.sub_agent_service import sub_agent_service
        from app.models.sub_agent import SubAgentRun
        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        row = await db.get(SubAgentRun, data.sub_agent_id)
        if row is None or (report is not None and str(row.report_id) != str(report.id)):
            yield ToolErrorEvent(type="tool.error", payload={"error": "No such sub-agent in this conversation.", "code": "NOT_FOUND"})
            return
        row = await sub_agent_service.stop_sub_agent(db, data.sub_agent_id, reason=data.reason or "stopped by the agent")
        yield ToolEndEvent(type="tool.end", payload={
            "output": {"sub_agent_id": str(row.id), "status": row.status, "title": row.title},
            "observation": {"summary": f"Sub-agent '{row.title}' is now {row.status}.", "success": True,
                            "sub_agent_id": str(row.id), "status": row.status, "title": row.title},
        })
