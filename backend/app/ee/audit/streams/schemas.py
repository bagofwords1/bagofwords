# Audit Log Stream schemas
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class StreamCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    destination: str
    config: Dict[str, Any] = Field(default_factory=dict)
    secrets: Dict[str, Any] = Field(default_factory=dict)
    action_filter: Optional[List[str]] = None
    start_from: Literal["now", "beginning"] = "now"
    activate: bool = True


class StreamUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    config: Optional[Dict[str, Any]] = None
    # Omitted keys and masked values ("••••…") keep the stored secret; an
    # empty string clears it.
    secrets: Optional[Dict[str, Any]] = None
    action_filter: Optional[List[str]] = None
    state: Optional[Literal["active", "inactive"]] = None


class StreamTestRequest(BaseModel):
    """Send one synthetic event with an unsaved form (and, when editing, the
    stored secrets of ``stream_id`` for fields left masked)."""
    destination: str
    config: Dict[str, Any] = Field(default_factory=dict)
    secrets: Dict[str, Any] = Field(default_factory=dict)
    stream_id: Optional[str] = None


class StreamTestResult(BaseModel):
    ok: bool
    kind: str
    error: Optional[str] = None
    status: Optional[int] = None


class StreamStatus(BaseModel):
    pending: int = 0
    lag_seconds: float = 0.0


class StreamResponse(BaseModel):
    id: str
    name: str
    destination: str
    config: Dict[str, Any]
    secrets: Dict[str, str]  # masked
    action_filter: Optional[List[str]] = None
    state: str
    start_from: str
    cursor_created_at: Optional[datetime] = None
    delivered_count: int = 0
    last_delivered_at: Optional[datetime] = None
    last_attempt_at: Optional[datetime] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    next_attempt_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    status: StreamStatus = Field(default_factory=StreamStatus)


class DestinationField(BaseModel):
    key: str
    kind: str
    required: bool
    secret: bool
    default: Any = None
    options: List[str] = Field(default_factory=list)
    advanced: bool = False


class DestinationSpec(BaseModel):
    type: str
    fields: List[DestinationField]
