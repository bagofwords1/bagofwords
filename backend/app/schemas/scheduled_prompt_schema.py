from typing import Optional, List
from datetime import datetime, timezone
from pydantic import BaseModel, field_validator
from app.utils.reasoning_effort import normalize_prompt_json_effort
from app.schemas.notification_schema import NotificationSubscriber


def _naive_utc(v: Optional[datetime]) -> Optional[datetime]:
    """Store instants as naive UTC, like every other DateTime column here."""
    if v is not None and v.tzinfo is not None:
        return v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


class ScheduledPromptCreate(BaseModel):
    prompt: dict  # PromptSchema-compatible JSON: {"content": "...", ...}
    title: Optional[str] = None
    # Exactly one of the two: a cron for a recurring task, or the instant a
    # one-time task runs at.
    cron_schedule: Optional[str] = None
    run_once_at: Optional[datetime] = None
    is_active: Optional[bool] = True
    # Routing: False = run in the host report (default), True = spawn a
    # fresh report per run.
    spawn_new_report: Optional[bool] = False
    notification_subscribers: Optional[List[NotificationSubscriber]] = None

    @field_validator("prompt", mode="after")
    @classmethod
    def _validate_prompt_effort(cls, v):
        return normalize_prompt_json_effort(v)

    @field_validator("run_once_at", mode="after")
    @classmethod
    def _normalize_run_once_at(cls, v):
        return _naive_utc(v)


class ScheduledPromptUpdate(BaseModel):
    prompt: Optional[dict] = None
    title: Optional[str] = None
    # Setting one switches the task to that kind: a cron makes it recurring,
    # run_once_at makes it one-time.
    cron_schedule: Optional[str] = None
    run_once_at: Optional[datetime] = None
    is_active: Optional[bool] = None
    spawn_new_report: Optional[bool] = None
    notification_subscribers: Optional[List[NotificationSubscriber]] = None

    @field_validator("prompt", mode="after")
    @classmethod
    def _validate_prompt_effort(cls, v):
        return normalize_prompt_json_effort(v)

    @field_validator("run_once_at", mode="after")
    @classmethod
    def _normalize_run_once_at(cls, v):
        return _naive_utc(v)


class ScheduledPromptSchema(BaseModel):
    id: str
    report_id: str
    title: Optional[str] = None
    prompt: dict
    cron_schedule: str
    run_once_at: Optional[datetime] = None
    is_active: bool
    spawn_new_report: bool = False
    last_run_at: Optional[datetime] = None
    # Filled from the live APScheduler job, not stored — None when paused.
    next_run_at: Optional[datetime] = None
    # Verdict of the most recent run, so a broken schedule is visible in the
    # list without opening it. Derived, not stored.
    last_run_status: Optional[str] = None
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


class ScheduledPromptRunSchema(BaseModel):
    """One past run of a scheduled task — the report it produced.

    Report-per-run mode gives one row per fire. In host-report mode every run
    lands in the same report, so the history is a single row (the schedule's
    `last_run_at` is the better freshness signal there).
    """
    report_id: str
    title: Optional[str] = None
    created_at: Optional[datetime] = None
    # Verdict of the run's last system turn: success | error | in_progress.
    # None when the run produced no system turn at all (it died before
    # answering) — the UI renders that as a failure, not as "no news".
    status: Optional[str] = None


class ScheduledPromptRunListResponse(BaseModel):
    runs: List[ScheduledPromptRunSchema]
    total: int = 0
    # False in host-report mode: the runs did not each get their own report.
    spawns_reports: bool = True
