from typing import Optional
from pydantic import BaseModel, Field


class CancelScheduledTaskInput(BaseModel):
    """Input schema for the cancel_scheduled_task tool.

    Cancels (deletes) a recurring scheduled task on the current report. The
    ``task_id`` comes from the <scheduled_tasks> context block, which lists the
    active tasks for this report.
    """

    task_id: str = Field(
        ...,
        min_length=1,
        description=(
            "The ID of the scheduled task to cancel. Take it from the "
            "<scheduled_tasks> block in context, which lists each active task's id."
        ),
    )


class CancelScheduledTaskOutput(BaseModel):
    """Output schema for the cancel_scheduled_task tool."""

    success: bool = Field(..., description="Whether the scheduled task was cancelled.")
    task_id: Optional[str] = Field(default=None, description="ID of the cancelled scheduled task.")
    error: Optional[str] = Field(default=None, description="Error message if cancellation failed.")
