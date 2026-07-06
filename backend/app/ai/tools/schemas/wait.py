from typing import Optional
from pydantic import BaseModel, Field


# Bounds for a single wait. A wait is a short, in-task pause-and-retry — not a
# standing automation — so we cap it at 24h. The 1-minute floor keeps a "wait"
# from degenerating into an immediate re-fire loop.
MIN_WAIT_MINUTES = 1
MAX_WAIT_MINUTES = 24 * 60  # 1440


class WaitInput(BaseModel):
    """Input schema for the wait tool.

    Pauses the current task for ``delay_minutes`` and then automatically resumes
    the agent on this report. Nothing else happens during the wait — no polling,
    no partial work. When the timer elapses, a fresh agent turn is started with
    ``reason`` as the instruction, and the full conversation history is reloaded,
    so the agent can retry exactly where it left off.
    """

    delay_minutes: int = Field(
        ...,
        ge=MIN_WAIT_MINUTES,
        le=MAX_WAIT_MINUTES,
        description=(
            "How long to wait before resuming, in MINUTES. Convert any hours to "
            f"minutes yourself (e.g. 'wait 2 hours' -> 120). Must be between "
            f"{MIN_WAIT_MINUTES} and {MAX_WAIT_MINUTES} (24h)."
        ),
    )
    reason: str = Field(
        ...,
        min_length=1,
        description=(
            "A short, self-contained instruction describing WHAT to do when the "
            "wait elapses. There is no user present when it resumes, so write it "
            "as a complete task, not a chat reply — e.g. 'Re-run the revenue "
            "export; the source table was mid-refresh.' The full conversation "
            "history is reloaded on resume, so you don't need to restate context."
        ),
    )


class WaitOutput(BaseModel):
    """Output schema for the wait tool."""

    status: str = Field(
        default="scheduled",
        description="'scheduled' when the wait is armed, 'cancelled' once the user cancels it.",
    )
    job_id: Optional[str] = Field(default=None, description="APScheduler job id for the one-shot resume.")
    wake_at: Optional[str] = Field(default=None, description="ISO-8601 UTC timestamp when the agent will resume.")
    delay_minutes: Optional[int] = Field(default=None, description="The delay that was scheduled, in minutes.")
    reason: Optional[str] = Field(default=None, description="The instruction that will run on resume.")
