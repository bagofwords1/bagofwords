from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.services.app_log_service import MAX_DURATION_MINUTES


class AppLogEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    timestamp: datetime
    instance_id: str
    level: Optional[str] = None
    logger: Optional[str] = None
    stream: str
    message: str


class AppLogList(BaseModel):
    """Newest first. ``has_more`` is true when the limit cut matching rows off."""

    items: List[AppLogEntry]
    limit: int
    has_more: bool


class AppLogSettings(BaseModel):
    enabled: bool
    # When a timed capture switches itself off; null while off or "until turned off".
    expires_at: Optional[datetime] = None
    # Hostname of the instance that answered this request.
    instance_id: str
    # Every instance that has rows in the table, for the instance filter.
    instances: List[str]
    retention_days: int
    max_size_bytes: int
    size_bytes: int


class AppLogSettingsUpdate(BaseModel):
    enabled: bool
    # Switch off again automatically after this many minutes; omit to keep
    # capturing until turned off. Ignored when disabling.
    duration_minutes: Optional[int] = Field(default=None, ge=1, le=MAX_DURATION_MINUTES)


class AppLogDeleteResult(BaseModel):
    deleted: int
