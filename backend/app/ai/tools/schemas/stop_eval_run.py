from typing import Optional
from pydantic import BaseModel, Field


class StopEvalRunInput(BaseModel):
    """Input schema for ``stop_eval_run``."""

    run_id: str = Field(..., description="The in-progress TestRun to stop.")


class StopEvalRunOutput(BaseModel):
    success: bool
    run_id: Optional[str] = None
    status: Optional[str] = None
    total: int = 0
    passed: int = 0
    failed: int = 0
    rejected_reason: Optional[str] = None
    message: Optional[str] = None
