"""Cancel Scheduled Task Tool - cancels a recurring task on the current report.

Thin wrapper over ScheduledPromptService.delete_scheduled_prompt (soft-delete +
APScheduler job removal). Guards that the task belongs to the current report so
the agent can't cancel another report's or org's task. The task_id comes from the
<scheduled_tasks> context block.
"""

from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel
import logging

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.cancel_scheduled_task import (
    CancelScheduledTaskInput,
    CancelScheduledTaskOutput,
)
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)

logger = logging.getLogger(__name__)


class CancelScheduledTaskTool(Tool):
    """Cancel a recurring scheduled task on the current report."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="cancel_scheduled_task",
            description=(
                "ACTION: Cancel (delete) a RECURRING scheduled task on the current "
                "report. Use this when the user asks to stop, cancel, or remove a "
                "scheduled task — 'stop emailing me weekly', 'cancel the daily refresh'. "
                "Find the task's id in the <scheduled_tasks> context block and pass it as "
                "task_id. Only tasks belonging to the current report can be cancelled."
            ),
            category="action",
            version="1.0.0",
            input_schema=CancelScheduledTaskInput.model_json_schema(),
            output_schema=CancelScheduledTaskOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=True,
            required_permissions=[],
            tags=["scheduling", "task", "automation", "action"],
            examples=[
                {
                    "input": {"task_id": "a1b2c3d4-0000-0000-0000-000000000000"},
                    "description": "Cancel the scheduled task with this id (taken from <scheduled_tasks>).",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CancelScheduledTaskInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CancelScheduledTaskOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = CancelScheduledTaskInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {str(e)}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(type="tool.start", payload={"task_id": data.task_id})

        db = runtime_ctx.get("db")
        user = runtime_ctx.get("user")
        report = runtime_ctx.get("report")
        organization = runtime_ctx.get("organization")

        if not db or not user or not report or not organization:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": CancelScheduledTaskOutput(
                        success=False,
                        task_id=data.task_id,
                        error="Cancelling scheduled tasks requires an active report context.",
                    ).model_dump(),
                    "observation": {
                        "summary": "Could not cancel scheduled task: missing report/user context.",
                        "success": False,
                        "artifacts": [],
                    },
                },
            )
            return

        try:
            from app.models.scheduled_prompt import ScheduledPrompt
            from app.services.scheduled_prompt_service import scheduled_prompt_service

            sp = await db.get(ScheduledPrompt, data.task_id)
            # Guard: must exist, not be deleted, and belong to the current report.
            if not sp or sp.deleted_at is not None or str(sp.report_id) != str(report.id):
                msg = "No active scheduled task with that id was found on this report."
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": CancelScheduledTaskOutput(
                            success=False, task_id=data.task_id, error=msg,
                        ).model_dump(),
                        "observation": {"summary": msg, "success": False, "artifacts": []},
                    },
                )
                return

            await scheduled_prompt_service.delete_scheduled_prompt(
                db=db,
                scheduled_prompt_id=str(sp.id),
                current_user=user,
                organization=organization,
            )

            summary = "Scheduled task cancelled."
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": CancelScheduledTaskOutput(
                        success=True, task_id=str(sp.id),
                    ).model_dump(),
                    "observation": {"summary": summary, "success": True, "artifacts": []},
                },
            )
        except Exception as e:
            logger.exception("Failed to cancel scheduled task: %s", e)
            detail = getattr(e, "detail", None) or str(e)
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": f"Failed to cancel scheduled task: {detail}",
                    "code": "CANCEL_FAILED",
                },
            )
