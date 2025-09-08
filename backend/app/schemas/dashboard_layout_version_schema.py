from pydantic import BaseModel, Field
from typing import List, Optional, Literal, Dict, Any
from datetime import datetime


class DashboardBlock(BaseModel):
    type: Literal["widget", "text_widget", "input_widget"]
    widget_id: Optional[str] = None
    text_widget_id: Optional[str] = None
    input_widget_id: Optional[str] = None
    x: int
    y: int
    width: int
    height: int


class DashboardLayoutVersionBase(BaseModel):
    name: str = ""
    version: int = 1
    is_active: bool = False
    theme_name: Optional[str] = None
    theme_overrides: Dict[str, Any] = Field(default_factory=dict)
    blocks: List[DashboardBlock] = Field(default_factory=list)


class DashboardLayoutVersionCreate(DashboardLayoutVersionBase):
    report_id: str


class DashboardLayoutVersionUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None
    theme_name: Optional[str] = None
    theme_overrides: Optional[Dict[str, Any]] = None
    blocks: Optional[List[DashboardBlock]] = None


class DashboardLayoutVersionSchema(DashboardLayoutVersionBase):
    id: str
    report_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


