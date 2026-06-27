"""Edit Scheduled Task Tool - edits an existing recurring task on the current report.

Thin wrapper over ScheduledPromptService.update_scheduled_prompt. Lets the agent
change a scheduled task's prompt, cron schedule, and/or active state in place
(rather than cancelling and re-creating). Guards that the task belongs to the
current report so the agent can't edit another report's or org's task. The
task_id comes from the <scheduled_tasks> context block.

Only the fields provided are changed; omitted fields are left as-is. The same
1-hour cron floor as create_scheduled_task is enforced when a new schedule is
given.
"""

from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel
import logging

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.edit_scheduled_task import (
    EditScheduledTaskInput,
    EditScheduledTaskOutput,
)
from app.ai.tools.implementations.create_scheduled_task import (
    _minute_field_is_single_value,
)
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)

logger = logging.getLogger(__name__)


class EditScheduledTaskTool(Tool):
    """Edit a recurring scheduled task on the current report."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="edit_scheduled_task",
            description=(
                "ACTION: Edit an existing RECURRING scheduled task on the current "
                "report — change its instruction, its schedule, or pause/resume it. "
                "Use this when the user wants to modify a task that already exists "
                "rather than create a new one — 'run the weekly digest on Fridays "
                "instead', 'also include refunds in that scheduled report', 'pause "
                "the daily refresh for now', 'change it to 7am'. Prefer this over "
                "cancel + create when the user is adjusting an existing task.\n\n"
                "Find the task's id in the <scheduled_tasks> context block and pass "
                "it as task_id. Only tasks belonging to the current report can be "
                "edited. Provide ONLY the fields you want to change — 'task_prompt' "
                "to rewrite the instruction (write the full self-contained prompt, "
                "not just the delta), 'cron_schedule' to change when it runs, "
                "'is_active' to pause (false) or resume (true). Omitted fields are "
                "left unchanged.\n\n"
                "Schedule: provide a 5-field cron expression "
                "(minute hour day-of-month month day-of-week). The minute field must "
                "be a single number — schedules more frequent than hourly are rejected."
            ),
            category="action",
            version="1.0.0",
            input_schema=EditScheduledTaskInput.model_json_schema(),
            output_schema=EditScheduledTaskOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=False,
            required_permissions=[],
            tags=["scheduling", "task", "automation", "action"],
            examples=[
                {
                    "input": {
                        "task_id": "a1b2c3d4-0000-0000-0000-000000000000",
                        "cron_schedule": "0 7 * * 5",
                    },
                    "description": "Move an existing task to run Fridays at 7am.",
                },
                {
                    "input": {
                        "task_id": "a1b2c3d4-0000-0000-0000-000000000000",
                        "task_prompt": (
                            "Pull yesterday's signups AND refunds and email me both counts."
                        ),
                    },
                    "description": "Rewrite the task's instruction (full prompt).",
                },
                {
                    "input": {
                        "task_id": "a1b2c3d4-0000-0000-0000-000000000000",
                        "is_active": False,
                    },
                    "description": "Pause a task without deleting it.",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return EditScheduledTaskInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return EditScheduledTaskOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = EditScheduledTaskInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {str(e)}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={"task_id": data.task_id, "cron_schedule": data.cron_schedule},
        )

        db = runtime_ctx.get("db")
        user = runtime_ctx.get("user")
        report = runtime_ctx.get("report")
        organization = runtime_ctx.get("organization")

        if not db or not user or not report or not organization:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": EditScheduledTaskOutput(
                        success=False,
                        task_id=data.task_id,
                        error="Editing scheduled tasks requires an active report context.",
                    ).model_dump(),
                    "observation": {
                        "summary": "Could not edit scheduled task: missing report/user context.",
                        "success": False,
                        "artifacts": [],
                    },
                },
            )
            return

        # Require at least one editable field so we don't issue a no-op write.
        if data.task_prompt is None and data.cron_schedule is None and data.is_active is None:
            msg = (
                "Nothing to edit: provide at least one of task_prompt, cron_schedule, "
                "or is_active."
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": EditScheduledTaskOutput(
                        success=False, task_id=data.task_id, error=msg,
                    ).model_dump(),
                    "observation": {"summary": msg, "success": False, "artifacts": []},
                },
            )
            return

        # Enforce the 1-hour floor when a new schedule is supplied.
        if data.cron_schedule is not None and not _minute_field_is_single_value(data.cron_schedule):
            msg = (
                "Invalid schedule: use a 5-field cron with a single-number minute "
                "(e.g. '0 9 * * 1'). Schedules more frequent than hourly are not allowed."
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": EditScheduledTaskOutput(
                        success=False,
                        task_id=data.task_id,
                        cron_schedule=data.cron_schedule,
                        error=msg,
                    ).model_dump(),
                    "observation": {"summary": msg, "success": False, "artifacts": []},
                },
            )
            return

        try:
            from app.models.scheduled_prompt import ScheduledPrompt
            from app.services.scheduled_prompt_service import scheduled_prompt_service
            from app.schemas.scheduled_prompt_schema import ScheduledPromptUpdate

            sp = await db.get(ScheduledPrompt, data.task_id)
            # Guard: must exist, not be deleted, and belong to the current report.
            if not sp or sp.deleted_at is not None or str(sp.report_id) != str(report.id):
                msg = "No active scheduled task with that id was found on this report."
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": EditScheduledTaskOutput(
                            success=False, task_id=data.task_id, error=msg,
                        ).model_dump(),
                        "observation": {"summary": msg, "success": False, "artifacts": []},
                    },
                )
                return

            # Build the prompt patch: preserve the existing prompt's other fields
            # (mode, model_id, mentions, ...) and only swap the content.
            prompt_patch = None
            if data.task_prompt is not None:
                prompt_patch = {**(sp.prompt or {}), "content": data.task_prompt}

            updated = await scheduled_prompt_service.update_scheduled_prompt(
                db=db,
                scheduled_prompt_id=str(sp.id),
                data=ScheduledPromptUpdate(
                    prompt=prompt_patch,
                    cron_schedule=data.cron_schedule,
                    is_active=data.is_active,
                ),
                current_user=user,
                organization=organization,
            )

            changed = []
            if data.task_prompt is not None:
                changed.append("prompt")
            if data.cron_schedule is not None:
                changed.append(f"schedule -> {updated.cron_schedule}")
            if data.is_active is not None:
                changed.append("resumed" if updated.is_active else "paused")
            summary = "Scheduled task updated (" + ", ".join(changed) + ")."

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": EditScheduledTaskOutput(
                        success=True,
                        task_id=str(updated.id),
                        cron_schedule=updated.cron_schedule,
                        is_active=updated.is_active,
                    ).model_dump(),
                    "observation": {"summary": summary, "success": True, "artifacts": []},
                },
            )
        except Exception as e:
            logger.exception("Failed to edit scheduled task: %s", e)
            detail = getattr(e, "detail", None) or str(e)
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": f"Failed to edit scheduled task: {detail}",
                    "code": "EDIT_FAILED",
                },
            )
