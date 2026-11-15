"""Request/response shapes for dashboard access requests."""
from typing import List, Optional

from pydantic import BaseModel, Field


class AccessRequestCreate(BaseModel):
    # The requester's optional message to the editors; trimmed/capped server-side.
    note: Optional[str] = Field(None, max_length=1000)


class AccessRequestBlockedSchema(BaseModel):
    """What the no-access page is told. Deliberately thin -- never the title."""
    owner_name: Optional[str] = None
    requests_enabled: bool = True
    eligible: bool = True
    has_approvers: bool = True
    pending: bool = False
    retry_at: Optional[str] = None


class PendingRequestSchema(BaseModel):
    id: str
    requester_name: Optional[str] = None
    requester_email: Optional[str] = None
    note: Optional[str] = None
    created_at: Optional[str] = None


class PendingRequestsSchema(BaseModel):
    requests: List[PendingRequestSchema] = []
    # True when the dashboard is private: approving switches it to "Specific
    # people" (the approval dialog says so).
    is_private: bool = False
    # True when the dashboard runs with its creator's data access: approving
    # lets the viewer see what the creator sees (the dialog warns about this).
    runs_as_creator: bool = False
    creator_name: Optional[str] = None


class AccessRequestDecisionSchema(BaseModel):
    id: str
    status: str
