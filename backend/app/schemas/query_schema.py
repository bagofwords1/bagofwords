from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from app.schemas.visualization_schema import VisualizationSchema
from app.schemas.step_schema import StepSchema


class QueryCreate(BaseModel):
    title: str
    description: Optional[str] = None
    report_id: Optional[str] = None
    widget_id: Optional[str] = None
    organization_id: Optional[str] = None
    user_id: Optional[str] = None


class QuerySchema(BaseModel):
    id: str
    title: str
    report_id: Optional[str] = None
    widget_id: str
    organization_id: Optional[str] = None
    user_id: Optional[str] = None
    default_step_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    visualizations: Optional[list[VisualizationSchema]] = None
    default_step: Optional[StepSchema] = None

    class Config:
        from_attributes = True


class QueryRunRequest(BaseModel):
    code: str
    title: Optional[str] = None
    data_model: Optional[dict] = None
    type: Optional[str] = None
    row_limit: Optional[int] = None
    tool_execution_id: Optional[str] = None


class PublicQuerySchema(BaseModel):
    """Minimal schema for public/unauthenticated access to published reports."""
    id: str
    title: str
    default_step_id: Optional[str] = None

    class Config:
        from_attributes = True


