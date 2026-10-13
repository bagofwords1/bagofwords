"""read_sub_agent — read a sub-agent's record (or progress snapshot while it runs)
and optionally its full write-up note. Parent-side, on demand."""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel, ValidationError

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import ToolEndEvent, ToolErrorEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.sub_agents import ReadSubAgentInput

logger = logging.getLogger(__name__)


class ReadSubAgentTool(Tool):

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_sub_agent",
            description=(
                "RESEARCH: Read one sub-agent by id (from <sub_agents> or a delegate receipt). While it runs you "
                "get a progress snapshot (status, steps used of budget, elapsed, the latest tool calls, "
                "evidence created so far, the note's first lines). Once finished you get its record and "
                "envelope. Set include_report=true to also get the child's full write-up note — do that when "
                "a finding is surprising, contradicts another sub-agent, or needs detail in your answer. "
                "Use it to answer 'how is it going' mid-wave; never to wait for a result."
            ),
            category="research",
            version="1.0.0",
            input_schema=ReadSubAgentInput.model_json_schema(),
            output_schema={"type": "object"},
            max_retries=1,
            timeout_seconds=20,
            idempotent=True,
            required_permissions=[],
            allowed_modes=["chat", "training"],
            observation_policy="always",
            digest_keys=["sub_agent_id", "title", "status", "outcome", "confidence", "steps_used"],
            tags=["sub-agent", "read", "progress"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ReadSubAgentInput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        try:
            data = ReadSubAgentInput(**(tool_input or {}))
        except ValidationError as e:
            yield ToolErrorEvent(type="tool.error", payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"})
            return
        yield ToolStartEvent(type="tool.start", payload={"sub_agent_id": data.sub_agent_id})
        from app.services.sub_agent_service import sub_agent_service
        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        snap = await sub_agent_service.snapshot(
            db, data.sub_agent_id, include_report=data.include_report,
            report_id=str(report.id) if report is not None else None,
        )
        if snap is None:
            yield ToolErrorEvent(type="tool.error", payload={"error": "No such sub-agent in this conversation.", "code": "NOT_FOUND"})
            return
        rec = snap.get("record") or {}
        if snap["status"] == "running":
            latest = "; ".join(f"{c['tool']}: {c['title']} ({c['status']})" for c in snap.get("latest_calls") or []) or "no tool calls yet"
            summary = (
                f"Sub-agent '{snap['title']}' is running: {snap['steps_used']}/{snap.get('max_steps')} steps, "
                f"{snap.get('elapsed_s', 0)}s elapsed. Latest: {latest}."
            )
        elif snap["status"] == "pending":
            summary = f"Sub-agent '{snap['title']}' is queued, waiting for a slot."
        else:
            summary = (
                f"Sub-agent '{snap['title']}' {snap['status']} ({snap.get('exit_reason')}): "
                f"{rec.get('outcome', '?')} (confidence {rec.get('confidence', '?')}) — {rec.get('summary', '')}"
            )
        obs = {"summary": summary, "success": True, **{k: v for k, v in snap.items() if k != "calls"}}
        obs["outcome"] = rec.get("outcome")
        obs["confidence"] = rec.get("confidence")
        yield ToolEndEvent(type="tool.end", payload={"output": snap, "observation": obs})
