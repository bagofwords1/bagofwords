from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class NoteSchema(BaseModel):
    """Full note representation for API responses."""
    id: str
    report_id: str
    organization_id: str
    agent_execution_id: Optional[str] = None
    user_id: Optional[str] = None
    title: Optional[str] = None
    content: str
    source: str = "agent"
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
