from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field, validator
import re


class NotificationChannel(str, Enum):
    EMAIL = "email"
    # Future channels:
    # SLACK = "slack"
    # TEAMS = "teams"
    # IN_APP = "in_app"


class NotificationType(str, Enum):
    SHARE_DASHBOARD = "share_dashboard"
    SHARE_CONVERSATION = "share_conversation"
    SCHEDULE_REPORT = "schedule_report"


class NotifyRequest(BaseModel):
    type: NotificationType
    channels: List[NotificationChannel]
    recipients: List[str] = Field(..., min_items=1, max_items=20)
    share_url: Optional[str] = None
    message: Optional[str] = Field(None, max_length=500)

    @validator("recipients", each_item=True)
    def validate_email(cls, v):
        pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        if not re.match(pattern, v):
            raise ValueError(f"Invalid email address: {v}")
        return v.lower().strip()

    @validator("recipients")
    def deduplicate(cls, v):
        return list(dict.fromkeys(v))


class ChannelResult(BaseModel):
    channel: str
    status: str  # "sent" or "failed"
    recipients: List[str] = []
    error: Optional[str] = None


class NotifyResponse(BaseModel):
    dispatched: List[ChannelResult] = []
    errors: List[ChannelResult] = []


class NotificationSubscriber(BaseModel):
    type: str  # "user" or "email"
    id: Optional[str] = None      # user ID (when type == "user")
    address: Optional[str] = None  # email address (when type == "email")

    @validator("type")
    def validate_type(cls, v):
        if v not in ("user", "email"):
            raise ValueError("type must be 'user' or 'email'")
        return v


class ScheduleRequest(BaseModel):
    cron_expression: str
    notification_subscribers: Optional[List[NotificationSubscriber]] = None
