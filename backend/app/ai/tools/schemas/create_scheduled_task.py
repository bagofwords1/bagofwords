from typing import Optional
from pydantic import BaseModel, Field


class CreateScheduledTaskInput(BaseModel):
    """Input schema for the create_scheduled_task tool.

    Schedules a recurring task on the current report. When the task fires, the
    agent re-runs ``task_prompt`` autonomously (with no user present) and uses
    whatever tools the task needs.

    Email is automatic: prompts that ask to email/notify the user are delivered
    by the send_email tool during the run; prompts that don't get a static
    summary email of the results. Either way the user is notified once.
    """

    task_prompt: str = Field(
        ...,
        min_length=1,
        description=(
            "The full, self-contained instruction to run on each scheduled "
            "execution. There is no user present when it runs, so write it as a "
            "complete task, not a chat reply. You do NOT need to add 'email me' "
            "just to notify the user — that happens automatically. Only state "
            "email intent explicitly when the message should be CONDITIONAL on the "
            "result (e.g. 'check for unusual activity this week and email me only "
            "if something looks off'); the agent will then handle it via send_email."
        ),
    )
    cron_schedule: str = Field(
        ...,
        description=(
            "A standard 5-field cron expression: 'minute hour day month day_of_week'. "
            "The minute field MUST be a single number (0-59) — sub-hourly schedules "
            "are not allowed (minimum interval is 1 hour). The day_of_week field "
            "accepts a comma-separated list or range (0=Sunday ... 6=Saturday) to "
            "target specific days. Examples: "
            "'0 9 * * 1' = every Monday at 09:00, '0 8 * * *' = every day at 08:00, "
            "'0 9 * * 1,3,5' = Mon/Wed/Fri at 09:00, '0 8 * * 1-5' = weekdays at 08:00, "
            "'30 7 1 * *' = 07:30 on the 1st of every month, '0 * * * *' = hourly."
        ),
    )


class CreateScheduledTaskOutput(BaseModel):
    """Output schema for the create_scheduled_task tool."""

    success: bool = Field(..., description="Whether the scheduled task was created.")
    task_id: Optional[str] = Field(default=None, description="ID of the created scheduled task.")
    cron_schedule: Optional[str] = Field(default=None, description="The cron expression that was scheduled.")
    error: Optional[str] = Field(default=None, description="Error message if creation failed.")
