from pydantic import BaseModel
from typing import List, Optional, Literal
from .widget_schema import WidgetSchema, WidgetCreate
from app.schemas.user_schema import UserSchema
from datetime import datetime
from app.schemas.data_source_schema import DataSourceReportSchema
from app.schemas.external_platform_schema import ExternalPlatformSchema

class ReportBase(BaseModel):
    title: str

class ReportCreate(ReportBase):
    widget: Optional[WidgetCreate] = None
    files: Optional[List[str]] = []
    data_sources: Optional[List[str]] = []
    external_platform_id: Optional[str] = None

class ReportUpdate(ReportBase):
    status: Optional[Literal["draft", "published", "archived"]] = None
    cron_schedule: Optional[str] = None

class ReportSchema(ReportBase):
    id: str
    status: Literal["draft", "published", "archived"]
    slug: str
    widgets: List[WidgetSchema] = []
    data_sources: List[DataSourceReportSchema] = []
    external_platform: Optional[ExternalPlatformSchema] = None
    user: UserSchema
    created_at: datetime
    updated_at: datetime
    cron_schedule: Optional[str] = None

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