from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class TestRunBatchCreate(BaseModel):
    case_ids: Optional[List[str]] = None
    suite_id: Optional[str] = None
    trigger_reason: Optional[str] = "manual"
    # Build system: optionally specify which instruction build to use
    # If None, uses the current main build (is_main=True)
    build_id: Optional[str] = None


class TestRunResponse(BaseModel):
    """Response schema for test run with build info."""
    id: str
    title: str
    suite_ids: str
    requested_by_user_id: Optional[str] = None
    trigger_reason: Optional[str] = None
    status: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    summary_json: Optional[dict] = None
    # Build system
    build_id: Optional[str] = None
    build_number: Optional[int] = None  # Denormalized for display
    
    class Config:
        from_attributes = True

