from pydantic import BaseModel
from typing import List, Optional, Literal
from .widget_schema import WidgetSchema, WidgetCreate
from app.schemas.user_schema import UserSchema
from datetime import datetime
from app.schemas.data_source_schema import DataSourceReportSchema
from app.schemas.external_platform_schema import ExternalPlatformSchema
from app.schemas.dashboard_layout_version_schema import DashboardLayoutVersionSchema

class ReportBase(BaseModel):
    title: Optional[str] = None

class ReportCreate(ReportBase):
    widget: Optional[WidgetCreate] = None
    files: Optional[List[str]] = []
    data_sources: Optional[List[str]] = []
    external_platform_id: Optional[str] = None

class ReportUpdate(BaseModel):
    title: Optional[str] = None
    status: Optional[Literal["draft", "published", "archived"]] = None
    theme_name: Optional[str] = None
    theme_overrides: Optional[dict] = None
    cron_schedule: Optional[str] = None
    data_sources: Optional[List[str]] = None
    mode: Optional[Literal["chat", "deep", "training"]] = None

class ReportSchema(ReportBase):
    class PublicGeneralSettings(BaseModel):
        ai_analyst_name: str = "AI Analyst"
        bow_credit: bool = True
        icon_url: Optional[str] = None

    id: str
    status: Literal["draft", "published", "archived"]
    slug: str
    report_type: Literal["regular", "test"]
    widgets: List[WidgetSchema] = []
    dashboard_layout_versions: List[DashboardLayoutVersionSchema] = []
    data_sources: List[DataSourceReportSchema] = []
    external_platform: Optional[ExternalPlatformSchema] = None
    user: UserSchema
    created_at: datetime
    updated_at: datetime
    last_run_at: Optional[datetime] = None
    cron_schedule: Optional[str] = None
    app_version: Optional[str] = None  # Version for routing decisions
    general: Optional[PublicGeneralSettings] = None
    theme_name: Optional[str] = None
    theme_overrides: Optional[dict] = None
    mode: Literal["chat", "deep", "training"] = "chat"
    # Conversation sharing
    conversation_share_enabled: bool = False
    conversation_share_token: Optional[str] = None

    class Config:
        from_attributes = True

class PaginationMeta(BaseModel):
    total: int
    page: int
    limit: int
    total_pages: int
    has_next: bool
    has_prev: bool

class ReportListResponse(BaseModel):
    reports: List[ReportSchema]
    meta: PaginationMeta