from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel
from app.schemas.notification_schema import NotificationSubscriber


class ScheduledPromptCreate(BaseModel):
    prompt: dict  # PromptSchema-compatible JSON: {"content": "...", ...}
    cron_schedule: str
    is_active: Optional[bool] = True
    notification_subscribers: Optional[List[NotificationSubscriber]] = None


class ScheduledPromptUpdate(BaseModel):
    prompt: Optional[dict] = None
    cron_schedule: Optional[str] = None
    is_active: Optional[bool] = None
    notification_subscribers: Optional[List[NotificationSubscriber]] = None


class ScheduledPromptSchema(BaseModel):
    id: str
    report_id: str
    prompt: dict
    cron_schedule: str
    is_active: bool
    last_run_at: Optional[datetime] = None
    notification_subscribers: Optional[list] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ScheduledPromptReportInfo(BaseModel):
    id: str
    title: Optional[str] = None

    class Config:
        from_attributes = True


class ScheduledPromptWithReport(ScheduledPromptSchema):
    report: Optional[ScheduledPromptReportInfo] = None
    user_name: Optional[str] = None


class ScheduledPromptListResponse(BaseModel):
    scheduled_prompts: List[ScheduledPromptWithReport]
    meta: dict
