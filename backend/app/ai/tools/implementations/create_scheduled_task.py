"""Create Scheduled Task Tool - schedules a recurring task on the current report.

This is a thin wrapper over ScheduledPromptService. It stores ``task_prompt`` as
a recurring prompt and registers an APScheduler cron job. When the job fires, the
agent re-runs that prompt autonomously and picks whatever tools it needs —
including send_email — so the email content is whatever the agent decides to
write, exactly like a live chat. Nothing email-specific is baked in here.

The recipient/owner is always the requesting user, and the task always belongs to
the current report (both resolved from runtime context).
"""

from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel
import logging

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.create_scheduled_task import (
    CreateScheduledTaskInput,
    CreateScheduledTaskOutput,
)
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)

logger = logging.getLogger(__name__)


def _minute_field_is_single_value(cron_schedule: str) -> bool:
    """Enforce the 1-hour floor: the minute field must be a single 0-59 value.

    A single integer minute guarantees the job fires at most once per hour
    regardless of the hour/day/month/dow fields, so '*', '*/n', lists ('0,30')
    and ranges ('0-30') in the minute field are rejected.
    """
    parts = (cron_schedule or "").split()
    if len(parts) != 5:
        return False
    minute = parts[0]
    if not minute.isdigit():
        return False
    return 0 <= int(minute) <= 59


class CreateScheduledTaskTool(Tool):
    """Schedule a recurring task (prompt) on the current report."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_scheduled_task",
            description=(
                "ACTION: Schedule a RECURRING task on the current report. Use this when "
                "the user asks for something to happen on a repeating schedule — "
                "'every week', 'each morning', 'remind me daily', 'on the 1st of the "
                "month', 'send me an email once a week about X'. For a one-off email "
                "right now, use send_email instead; this tool is only for recurring work.\n\n"
                "How it runs: on each scheduled fire, the agent re-runs your "
                "'task_prompt' on this report with NO user present, and uses whatever "
                "tools it needs autonomously. So if the user wants to be notified, write "
                "that into task_prompt explicitly (e.g. '...and email me a short summary'). "
                "send_email will be available during the run and the agent will call it.\n\n"
                "Schedule: provide a 5-field cron expression. The minute field must be a "
                "single number — schedules more frequent than hourly are rejected.\n\n"
                "Before creating, check the <scheduled_tasks> context block so you don't "
                "create a duplicate of an existing task."
            ),
            category="action",
            version="1.0.0",
            input_schema=CreateScheduledTaskInput.model_json_schema(),
            output_schema=CreateScheduledTaskOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=False,
            required_permissions=[],
            tags=["scheduling", "task", "automation", "action"],
            examples=[
                {
                    "input": {
                        "task_prompt": (
                            "Check for unusual or suspicious activity over the past week. "
                            "If you find anything notable, email me a short summary; "
                            "otherwise email me a one-line all-clear."
                        ),
                        "cron_schedule": "0 9 * * 1",
                    },
                    "description": "Weekly 'weird activity' digest emailed to the user.",
                },
                {
                    "input": {
                        "task_prompt": "Refresh the revenue dashboard with the latest data.",
                        "cron_schedule": "0 7 * * *",
                    },
                    "description": "Daily dashboard refresh (no email needed).",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreateScheduledTaskInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreateScheduledTaskOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = CreateScheduledTaskInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {str(e)}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={"cron_schedule": data.cron_schedule},
        )

        db = runtime_ctx.get("db")
        user = runtime_ctx.get("user")
        report = runtime_ctx.get("report")
        organization = runtime_ctx.get("organization")

        if not db or not user or not report or not organization:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": CreateScheduledTaskOutput(
                        success=False,
                        error="Scheduled tasks require an active report context.",
                    ).model_dump(),
                    "observation": {
                        "summary": "Could not create scheduled task: missing report/user context.",
                        "success": False,
                        "artifacts": [],
                    },
                },
            )
            return

        # Enforce the 1-hour floor before hitting the service.
        if not _minute_field_is_single_value(data.cron_schedule):
            msg = (
                "Invalid schedule: use a 5-field cron with a single-number minute "
                "(e.g. '0 9 * * 1'). Schedules more frequent than hourly are not allowed."
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": CreateScheduledTaskOutput(
                        success=False,
                        cron_schedule=data.cron_schedule,
                        error=msg,
                    ).model_dump(),
                    "observation": {"summary": msg, "success": False, "artifacts": []},
                },
            )
            return

        try:
            from app.services.scheduled_prompt_service import scheduled_prompt_service
            from app.schemas.scheduled_prompt_schema import ScheduledPromptCreate

            sp = await scheduled_prompt_service.create_scheduled_prompt(
                db=db,
                report_id=str(report.id),
                data=ScheduledPromptCreate(
                    prompt={"content": data.task_prompt},
                    cron_schedule=data.cron_schedule,
                    is_active=True,
                    notification_subscribers=None,
                ),
                current_user=user,
                organization=organization,
            )

            summary = (
                f"Scheduled task created (cron: {data.cron_schedule}). It will run on "
                f"this report and act autonomously each time."
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": CreateScheduledTaskOutput(
                        success=True,
                        task_id=str(sp.id),
                        cron_schedule=data.cron_schedule,
                    ).model_dump(),
                    "observation": {"summary": summary, "success": True, "artifacts": []},
                },
            )
        except Exception as e:
            logger.exception("Failed to create scheduled task: %s", e)
            detail = getattr(e, "detail", None) or str(e)
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": f"Failed to create scheduled task: {detail}",
                    "code": "CREATE_FAILED",
                },
            )
