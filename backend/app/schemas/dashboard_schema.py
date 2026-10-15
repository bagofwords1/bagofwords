"""Schemas of the dashboard routes (/api/d and /api/dashboards, phase 4)."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.notification_schema import NotificationSubscriber

Visibility = Literal["none", "shared", "internal", "public"]


class PublicGeneralSettings(BaseModel):
    ai_analyst_name: str = "AI Analyst"
    bow_credit: bool = True
    icon_url: str | None = None


class PublishedDashboardSchema(BaseModel):
    """What a /d page needs about the dashboard: nothing of its settings
    beyond what it shows (who it is shared with, schedules and chat sources
    are for the owner only)."""
    id: str
    slug: str | None = None
    title: str | None = None
    mode: str
    release_id: str
    published_at: datetime | None = None
    last_run_at: datetime | None = None
    theme_name: str | None = None
    theme_overrides: dict | None = None
    include_data_tab: bool
    chat_enabled: bool
    general: PublicGeneralSettings | None = None


class DraftDashboardSchema(BaseModel):
    id: str
    slug: str | None = None
    title: str | None = None
    mode: str
    report_id: str | None = None
    version_id: str | None = None
    published: bool
    theme_name: str | None = None
    theme_overrides: dict | None = None


class DashboardSharingSchema(BaseModel):
    # str, not Visibility: reports the stored value even if it is not one
    # the API writes, rather than failing the owner's read.
    visibility: str
    published: bool
    user_ids: list[str]
    group_ids: list[str]


class DashboardSharingUpdate(BaseModel):
    """None leaves that kind of grant as it is; leaving 'shared' clears all."""
    visibility: Visibility
    user_ids: list[str] | None = None
    group_ids: list[str] | None = None


class DashboardSettingsSchema(BaseModel):
    shared_run_identity: Literal["viewer", "creator"]
    include_data_tab: bool
    chat_enabled: bool
    chat_data_source_ids: list[str] | None = None
    chat_model_id: str | None = None
    cron_schedule: str | None = None
    refresh_on_view: bool = True
    notification_subscribers: list[dict] | None = None


class DashboardSettingsUpdate(BaseModel):
    """None leaves a setting as it is. chat_data_source_ids ["*"] and
    chat_model_id "" go back to inheriting from the conversation."""
    shared_run_identity: Literal["viewer", "creator"] | None = None
    include_data_tab: bool | None = None
    chat_enabled: bool | None = None
    chat_data_source_ids: list[str] | None = None
    chat_model_id: str | None = None
    # The schedule (phase 6). cron_schedule "" unschedules (and clears the
    # subscribers); refresh_on_view is separate and stays.
    cron_schedule: str | None = None
    refresh_on_view: bool | None = None
    notification_subscribers: list[NotificationSubscriber] | None = None


class DashboardReleaseSchema(BaseModel):
    id: str
    status: Literal["publishing", "ready", "failed"]
    error: str | None = None
    source_version_id: str | None = None
    published_by: str | None = None
    started_at: datetime | None = None
    published_at: datetime | None = None


class DashboardPublishStatusSchema(BaseModel):
    """The live release, the latest attempt (the live one, or a publish
    running or failed since), and whether the draft's newest completed
    version is not the one that is live."""
    published: DashboardReleaseSchema | None = None
    latest: DashboardReleaseSchema | None = None
    has_unpublished_changes: bool
    last_run: "DashboardRunSchema | None" = None


class DashboardRunSchema(BaseModel):
    """How the last refresh of the live release went (phase 6): `at` is the
    last success; status and error describe the last attempt."""
    at: datetime | None = None
    status: Literal["success", "failed"] | None = None
    error: str | None = None


class DashboardForkRequest(BaseModel):
    title: str | None = None


class DashboardPublishRequest(BaseModel):
    """None publishes the newest completed version."""
    version_id: str | None = None


DashboardPublishStatusSchema.model_rebuild()
