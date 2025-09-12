from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, Literal

from app.schemas.view_schema import ViewSchema


class VisualizationBase(BaseModel):
    title: str = ""
    status: str = "draft"
    report_id: str
    query_id: str
    # Keep spec for compatibility, but prefer view.type + view.encoding going forward
    view: ViewSchema = Field(default_factory=ViewSchema)


class VisualizationCreate(VisualizationBase):
    pass


class VisualizationUpdate(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None
    view: Optional[ViewSchema] = None


class VisualizationSchema(VisualizationBase):
    id: str

    class Config:
        from_attributes = True


class VisualizationSpec(BaseModel):
    type: Literal[
        "table",
        "bar_chart",
        "line_chart",
        "pie_chart",
        "area_chart",
        "count",
        "heatmap",
        "map",
        "candlestick",
        "treemap",
        "radar_chart",
        "scatter_plot",
    ] = Field(..., description="Visualization/data type")
    