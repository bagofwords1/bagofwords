"""Cancel Wait Tool — disarm pending wait wake-up(s) on this conversation.

The counterpart to ``wait``: use it when the user says "never mind" /
"stop checking", or when a newer task supersedes the pending wake-up.
With no ``job_id`` it cancels every pending wait on the current report
(the common intent). Also stamps ``status: "cancelled"`` onto the
originating wait tool-execution's ``result_json`` so the chat card stops
showing a live countdown — the same contract as the HTTP cancel endpoint
(``CompletionService.cancel_wait``).
"""
from typing import Any, AsyncIterator, Dict, Type
import logging

from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)
from app.ai.tools.schemas.cancel_wait import (
    CancelledWait,
    CancelWaitInput,
    CancelWaitOutput,
)

logger = logging.getLogger(__name__)


class CancelWaitTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="cancel_wait",
            description=(
                "ACTION: Cancel pending wait wake-up(s) armed by the wait tool "
                "on this conversation, so the agent is NOT resumed later. Use "
                "when the user asks to stop waiting/checking, or when a newer "
                "task supersedes the pending wake-up. Omit job_id to cancel "
                "every pending wait on this conversation. Idempotent — "
                "cancelling nothing succeeds with an empty list."
            ),
            category="action",
            version="1.0.0",
            input_schema=CancelWaitInput.model_json_schema(),
            output_schema=CancelWaitOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=15,
            idempotent=True,
            required_permissions=[],
            tags=["wait", "cancel", "action"],
            examples=[
                {
                    "input": {},
                    "description": "User said 'never mind, stop checking' — cancel all pending waits",
                },
                {
                    "input": {"job_id": "wait:<report-id>:<token>"},
                    "description": "Cancel one specific wait, keeping others armed",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CancelWaitInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CancelWaitOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = CancelWaitInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(type="tool.start", payload={"job_id": data.job_id})

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        if report is None:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": CancelWaitOutput(
                        success=False, message="Cannot cancel waits: no active report context."
                    ).model_dump(),
                    "observation": {"summary": "cancel_wait rejected: no report context", "artifacts": []},
                },
            )
            return

        try:
            from app.services.wait_service import wait_service

            report_id = str(report.id)
            pending = wait_service.list_waits(report_id)
            targets = pending
            if data.job_id:
                targets = [j for j in pending if j["job_id"] == data.job_id]
                # Allow cancelling a job that list_waits missed (e.g. store
                # hiccup) as long as it belongs to this report.
                if not targets and data.job_id.startswith(f"wait:{report_id}:"):
                    targets = [{"job_id": data.job_id, "wake_at": None, "reason": None}]

            cancelled: list[CancelledWait] = []
            for j in targets:
                if wait_service.cancel_wait(j["job_id"]):
                    cancelled.append(CancelledWait(**j))

            # Reflect the cancellation on the originating wait tool-execution
            # rows so the chat card stops rendering a live countdown.
            if cancelled and db is not None:
                try:
                    from app.models.agent_execution import AgentExecution
                    from app.models.tool_execution import ToolExecution

                    cancelled_ids = {c.job_id for c in cancelled}
                    rows = (
                        await db.execute(
                            select(ToolExecution)
                            .join(AgentExecution, AgentExecution.id == ToolExecution.agent_execution_id)
                            .where(AgentExecution.report_id == report_id)
                            .where(ToolExecution.tool_name == "wait")
                        )
                    ).scalars().all()
                    changed = False
                    for te in rows:
                        rj = dict(te.result_json or {})
                        if rj.get("job_id") in cancelled_ids and rj.get("status") != "cancelled":
                            rj["status"] = "cancelled"
                            te.result_json = rj
                            db.add(te)
                            changed = True
                    if changed:
                        await db.commit()
                except Exception as stamp_err:
                    logger.warning(f"cancel_wait: failed to stamp tool executions: {stamp_err}")

            if cancelled:
                msg = f"Cancelled {len(cancelled)} pending wait(s); the agent will not auto-resume."
            elif data.job_id:
                msg = f"No pending wait found for {data.job_id} (already fired or cancelled)."
            else:
                msg = "No pending waits on this conversation."

            output = CancelWaitOutput(success=True, cancelled=cancelled, message=msg)
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {"summary": msg, "artifacts": []},
                },
            )
        except Exception as e:
            logger.exception(f"cancel_wait failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Cancel failed: {e}", "code": "CANCEL_FAILED"},
            )
