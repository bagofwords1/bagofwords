from typing import List, Optional
from pydantic import BaseModel, Field


class CancelWaitInput(BaseModel):
    """Input schema for ``cancel_wait``.

    ``job_id`` targets one specific pending wait; omitted, every pending
    wait on the current report is cancelled — the common intent behind
    "stop checking".
    """

    job_id: Optional[str] = Field(
        default=None,
        description=(
            "The wait job to cancel (from the wait tool's output). Omit to "
            "cancel ALL pending waits on this conversation."
        ),
    )


class CancelledWait(BaseModel):
    job_id: str
    wake_at: Optional[str] = None
    reason: Optional[str] = None


class CancelWaitOutput(BaseModel):
    success: bool
    cancelled: List[CancelledWait] = Field(default_factory=list)
    message: Optional[str] = None
