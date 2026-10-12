"""stop_sub_run — stop one running sub-run (the wave continues)."""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel, ValidationError

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import ToolEndEvent, ToolErrorEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.sub_runs import StopSubRunInput

logger = logging.getLogger(__name__)


class StopSubRunTool(Tool):

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="stop_sub_run",
            description=(
                "ACTION: Stop one running sub-run by id when the user asks ('stop the pricing one') or its "
                "goal became moot. It is recorded as interrupted with a partial record; the rest of its "
                "wave keeps running and still reports back."
            ),
            category="action",
            version="1.0.0",
            input_schema=StopSubRunInput.model_json_schema(),
            output_schema={"type": "object"},
            max_retries=0,
            timeout_seconds=20,
            idempotent=True,
            required_permissions=[],
            allowed_modes=["chat", "training"],
            observation_policy="always",
            tags=["sub-run", "stop"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return StopSubRunInput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        try:
            data = StopSubRunInput(**(tool_input or {}))
        except ValidationError as e:
            yield ToolErrorEvent(type="tool.error", payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"})
            return
        yield ToolStartEvent(type="tool.start", payload={"sub_run_id": data.sub_run_id})
        from app.services.sub_run_service import sub_run_service
        from app.models.sub_run import SubRun
        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        row = await db.get(SubRun, data.sub_run_id)
        if row is None or (report is not None and str(row.report_id) != str(report.id)):
            yield ToolErrorEvent(type="tool.error", payload={"error": "No such sub-run in this conversation.", "code": "NOT_FOUND"})
            return
        row = await sub_run_service.stop_sub_run(db, data.sub_run_id, reason=data.reason or "stopped by the agent")
        yield ToolEndEvent(type="tool.end", payload={
            "output": {"sub_run_id": str(row.id), "status": row.status, "title": row.title},
            "observation": {"summary": f"Sub-run '{row.title}' is now {row.status}.", "success": True,
                            "sub_run_id": str(row.id), "status": row.status, "title": row.title},
        })
