from pydantic import BaseModel, Field
from typing import List, Optional, Literal, Dict, Any
from datetime import datetime


class FilterControl(BaseModel):
    kind: Literal["select", "multi", "daterange", "search", "checkbox", "radio"]
    label: Optional[str] = None
    options: Optional[List[Dict[str, Any]]] = None  # e.g., [{label, value}]
    default: Optional[Any] = None
    placeholder: Optional[str] = None


class FilterBinding(BaseModel):
    scope: Literal["report", "container", "explicit"] = "report"
    targets: Optional[List[str]] = None  # list of widget_ids if explicit
    tags: Optional[List[str]] = None     # alternative grouping selector
    param_key: str                       # the parameter name consumed by widgets


class ContainerLayout(BaseModel):
    cols: Optional[int] = None
    row_height: Optional[int] = None
    margin: Optional[List[int]] = None  # [x, y]


class ViewOverrides(BaseModel):
    # Partial ViewSchema fields without importing to avoid circulars
    component: Optional[str] = None
    variant: Optional[str] = None
    theme: Optional[str] = None
    style: Dict[str, Any] = Field(default_factory=dict)
    options: Dict[str, Any] = Field(default_factory=dict)


class ContainerChrome(BaseModel):
    title: Optional[str] = None
    showHeader: Optional[bool] = None
    border: Optional[Literal["none", "soft", "strong"]] = None
    padding: Optional[int] = None
    zIndex: Optional[int] = None


class BaseBlock(BaseModel):
    x: int
    y: int
    width: int
    height: int


class WidgetBlock(BaseBlock):
    type: Literal["widget"] = "widget"
    widget_id: str
    # Optional embedded widget payload when layouts are hydrated
    widget: Optional["WidgetSchema"] = None
    view_overrides: Optional[ViewOverrides] = None
    container: Optional[ContainerChrome] = None


class VisualizationBlock(BaseBlock):
    type: Literal["visualization"] = "visualization"
    visualization_id: str
    # Optional embedded visualization payload when layouts are hydrated
    visualization: Optional["VisualizationSchema"] = None
    view_overrides: Optional[ViewOverrides] = None
    container: Optional[ContainerChrome] = None


class TextWidgetBlock(BaseBlock):
    type: Literal["text_widget"] = "text_widget"
    text_widget_id: str
    # Optional embedded text widget payload when layouts are hydrated
    text_widget: Optional["TextWidgetSchema"] = None
    view_overrides: Optional[ViewOverrides] = None
    container: Optional[ContainerChrome] = None


class FilterBlock(BaseBlock):
    type: Literal["filter"] = "filter"
    control: FilterControl
    binding: FilterBinding


class ContainerBlock(BaseBlock):
    type: Literal["container"] = "container"
    title: Optional[str] = None
    theme_name: Optional[str] = None
    theme_overrides: Dict[str, Any] = Field(default_factory=dict)
    layout: Optional[ContainerLayout] = None
    children: List[WidgetBlock | TextWidgetBlock | FilterBlock] = Field(default_factory=list)  # relative positions
    container: Optional[ContainerChrome] = None


DashboardBlock = WidgetBlock | VisualizationBlock | TextWidgetBlock | FilterBlock | ContainerBlock


class DashboardLayoutVersionBase(BaseModel):
    name: str = ""
    version: int = 1
    is_active: bool = False
    theme_name: Optional[str] = None
    theme_overrides: Dict[str, Any] = Field(default_factory=dict)
    blocks: List[DashboardBlock] = Field(default_factory=list)


class BlockPositionPatch(BaseModel):
    type: Literal["widget", "visualization", "text_widget", "filter"]
    widget_id: Optional[str] = None
    visualization_id: Optional[str] = None
    text_widget_id: Optional[str] = None
    # filter identification could be extended later
    x: int
    y: int
    width: int
    height: int
    # Optional per-block view overrides to style at layout level
    view_overrides: Optional[ViewOverrides] = None


class DashboardLayoutBlocksPatch(BaseModel):
    blocks: List[BlockPositionPatch] = Field(default_factory=list)


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

# Late imports to avoid circular dependencies
from app.schemas.widget_schema import WidgetSchema  # noqa: E402
from app.schemas.text_widget_schema import TextWidgetSchema  # noqa: E402
from app.schemas.visualization_schema import VisualizationSchema  # noqa: E402


