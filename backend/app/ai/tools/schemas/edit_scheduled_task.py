from typing import Optional
from pydantic import BaseModel, Field


class EditScheduledTaskInput(BaseModel):
    """Input schema for the edit_scheduled_task tool.

    Edits an existing recurring scheduled task on the current report. The
    ``task_id`` comes from the <scheduled_tasks> context block, which lists the
    active tasks for this report. Only the fields you provide are changed; omit a
    field to leave it as-is. At least one editable field must be provided.
    """

    task_id: str = Field(
        ...,
        min_length=1,
        description=(
            "The ID of the scheduled task to edit. Take it from the "
            "<scheduled_tasks> block in context, which lists each active task's id."
        ),
    )
    task_prompt: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "The new full, self-contained instruction to run on each scheduled "
            "execution. There is no user present when it runs, so write it as a "
            "complete task, not a chat reply. Omit to keep the current prompt."
        ),
    )
    cron_schedule: Optional[str] = Field(
        default=None,
        description=(
            "A new standard 5-field cron expression: 'minute hour day month day_of_week'. "
            "The minute field MUST be a single number (0-59) — sub-hourly schedules "
            "are not allowed (minimum interval is 1 hour). The day_of_week field "
            "accepts a comma-separated list or range (0=Sunday ... 6=Saturday) to "
            "target specific days. Examples: "
            "'0 9 * * 1' = every Monday at 09:00, '0 8 * * *' = every day at 08:00, "
            "'0 9 * * 1,3,5' = Mon/Wed/Fri at 09:00, '0 8 * * 1-5' = weekdays at 08:00, "
            "'30 7 1 * *' = 07:30 on the 1st of every month, '0 * * * *' = hourly. "
            "Omit to keep the current schedule."
        ),
    )
    is_active: Optional[bool] = Field(
        default=None,
        description=(
            "Set to false to pause the task (it stays saved but stops firing) or "
            "true to resume it. Omit to leave its active state unchanged."
        ),
    )


class EditScheduledTaskOutput(BaseModel):
    """Output schema for the edit_scheduled_task tool."""

    success: bool = Field(..., description="Whether the scheduled task was updated.")
    task_id: Optional[str] = Field(default=None, description="ID of the edited scheduled task.")
    cron_schedule: Optional[str] = Field(default=None, description="The task's cron expression after the edit.")
    is_active: Optional[bool] = Field(default=None, description="The task's active state after the edit.")
    error: Optional[str] = Field(default=None, description="Error message if the edit failed.")
