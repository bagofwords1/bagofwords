from typing import Optional
from pydantic import BaseModel, Field


class CreateScheduledTaskInput(BaseModel):
    """Input schema for the create_scheduled_task tool.

    Schedules a recurring task on the current report. When the task fires, the
    agent re-runs ``task_prompt`` autonomously (with no user present) and uses
    whatever tools the task needs — including send_email to notify the user.
    """

    task_prompt: str = Field(
        ...,
        min_length=1,
        description=(
            "The full, self-contained instruction to run on each scheduled "
            "execution. There is no user present when it runs, so write it as a "
            "complete task, not a chat reply. If the user wants to be notified, "
            "say so explicitly here (e.g. 'check for unusual activity in the last "
            "week and email me a short summary of anything notable'). The agent "
            "will pick the tools it needs (including send_email) on its own."
        ),
    )
    cron_schedule: str = Field(
        ...,
        description=(
            "A standard 5-field cron expression: 'minute hour day month day_of_week'. "
            "The minute field MUST be a single number (0-59) — sub-hourly schedules "
            "are not allowed (minimum interval is 1 hour). Examples: "
            "'0 9 * * 1' = every Monday at 09:00, '0 8 * * *' = every day at 08:00, "
            "'30 7 1 * *' = 07:30 on the 1st of every month, '0 * * * *' = hourly."
        ),
    )
    schedule_description: str = Field(
        ...,
        min_length=1,
        max_length=120,
        description=(
            "A short, human-readable label for the schedule, shown in the UI and "
            "used to confirm back to the user. E.g. 'every Monday at 9am', "
            "'daily at 8am', 'on the 1st of each month at 7:30am'."
        ),
    )


class CreateScheduledTaskOutput(BaseModel):
    """Output schema for the create_scheduled_task tool."""

    success: bool = Field(..., description="Whether the scheduled task was created.")
    task_id: Optional[str] = Field(default=None, description="ID of the created scheduled task.")
    cron_schedule: Optional[str] = Field(default=None, description="The cron expression that was scheduled.")
    schedule_description: Optional[str] = Field(default=None, description="Human-readable schedule label.")
    error: Optional[str] = Field(default=None, description="Error message if creation failed.")
