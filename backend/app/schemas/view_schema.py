from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List


class ViewSchema(BaseModel):
    component: Optional[str] = None
    variant: Optional[str] = None
    theme: Optional[str] = None
    style: Dict[str, Any] = Field(default_factory=dict)
    options: Dict[str, Any] = Field(default_factory=dict)

    # Minimal spec-like keys merged into view for compatibility
    type: Optional[str] = None  # e.g., bar_chart, line_chart, table, etc.
    encoding: Optional["EncodingSchema"] = None  # Structured encoding; optional for back-compat

    # Common presentation flags used by EChartsVisual.vue
    legendVisible: Optional[bool] = None
    xAxisVisible: Optional[bool] = None
    yAxisVisible: Optional[bool] = None

    class Config:
        extra = "allow"



class SeriesEncodingSchema(BaseModel):
    name: Optional[str] = None
    value: Optional[str] = None
    # Common category/key field used for cartesian series
    key: Optional[str] = None

    class Config:
        # Allow additional fields for specialized charts (x,y,open,close,low,high, etc.)
        extra = "allow"


class EncodingSchema(BaseModel):
    # Categorical/metric mapping for bar/line/area
    category: Optional[str] = None
    value: Optional[str] = None
    series: Optional[List[SeriesEncodingSchema]] = None

    # Scatter/Cartesian
    x: Optional[str] = None
    y: Optional[str] = None
    color: Optional[str] = None
    size: Optional[str] = None

    # Heatmap
    # Uses x, y, value above

    # Candlestick
    time: Optional[str] = None
    open: Optional[str] = None
    high: Optional[str] = None
    low: Optional[str] = None
    close: Optional[str] = None

    # Treemap
    path: Optional[List[str]] = None
    id: Optional[str] = None
    parentId: Optional[str] = None
    name: Optional[str] = None

    # Radar
    dimensions: Optional[List[str]] = None

    class Config:
        extra = "allow"

