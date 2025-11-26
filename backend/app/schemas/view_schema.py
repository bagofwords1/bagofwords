from __future__ import annotations

from typing import Annotated, Dict, Any, List, Literal, Optional, Union

from pydantic import BaseModel, Field


# -----------------------------------------------------------------------------
# Palette definitions (shadcn-inspired)
# -----------------------------------------------------------------------------

SHADCN_THEMES: Dict[str, Dict[str, List[str]]] = {
    "default": {
        "primary": ["#2563eb", "#1d4ed8", "#1e40af"],
        "secondary": ["#16a34a", "#15803d", "#166534"],
        "accent": ["#db2777", "#be185d", "#9d174d"],
    },
    "red": {
        "primary": ["#ef4444", "#dc2626", "#b91c1c"],
        "secondary": ["#fb7185", "#e11d48", "#be123c"],
        "accent": ["#fecaca", "#f87171", "#b91c1c"],
    },
    "orange": {
        "primary": ["#f97316", "#ea580c", "#c2410c"],
        "secondary": ["#fdba74", "#f97316", "#c2410c"],
        "accent": ["#fed7aa", "#fb923c", "#ea580c"],
    },
    "yellow": {
        "primary": ["#facc15", "#eab308", "#ca8a04"],
        "secondary": ["#fcd34d", "#fbbf24", "#d97706"],
        "accent": ["#fef08a", "#facc15", "#b45309"],
    },
    "green": {
        "primary": ["#22c55e", "#16a34a", "#166534"],
        "secondary": ["#bbf7d0", "#4ade80", "#15803d"],
        "accent": ["#86efac", "#22c55e", "#14532d"],
    },
    "blue": {
        "primary": ["#3b82f6", "#2563eb", "#1e3a8a"],
        "secondary": ["#bfdbfe", "#60a5fa", "#1d4ed8"],
        "accent": ["#93c5fd", "#3b82f6", "#1d4ed8"],
    },
    "rose": {
        "primary": ["#f43f5e", "#e11d48", "#be123c"],
        "secondary": ["#fecdd3", "#fb7185", "#be123c"],
        "accent": ["#fda4af", "#f43f5e", "#be123c"],
    },
    "violet": {
        "primary": ["#a855f7", "#9333ea", "#6b21a8"],
        "secondary": ["#ede9fe", "#c4b5fd", "#7c3aed"],
        "accent": ["#ddd6fe", "#a855f7", "#6b21a8"],
    },
}


def _resolve_theme_colors(theme: str, scale: str) -> List[str]:
    theme_colors = SHADCN_THEMES.get(theme) or SHADCN_THEMES["default"]
    return theme_colors.get(scale) or theme_colors["primary"]


class GradientConfig(BaseModel):
    start: str = Field(alias="from")
    end: str = Field(alias="to")
    opacity: Optional[List[float]] = None

    class Config:
        populate_by_name = True
        extra = "allow"


class Palette(BaseModel):
    theme: Literal[
        "default",
        "red",
        "rose",
        "orange",
        "yellow",
        "green",
        "blue",
        "violet",
    ] = "default"
    scale: Literal["primary", "secondary", "accent"] = "primary"
    custom: Optional[List[str]] = None

    def resolve(self, series_count: int) -> List[str]:
        colors = self.custom or _resolve_theme_colors(self.theme, self.scale)
        if not colors:
            colors = _resolve_theme_colors("default", "primary")
        repeated = (colors * ((series_count // len(colors)) + 1))[:series_count]
        return repeated or colors


class AxisOptions(BaseModel):
    show: bool = True
    label: Optional[str] = None
    rotate: int = 45
    interval: int = 0
    format: Optional[str] = None


class LegendOptions(BaseModel):
    show: bool = True
    position: Literal["top", "bottom", "left", "right"] = "bottom"


class SeriesStyle(BaseModel):
    key: str
    label: Optional[str] = None
    color: Optional[str] = None
    gradient: Optional[GradientConfig] = None
    showValues: Optional[bool] = None


class BaseView(BaseModel):
    type: str
    title: Optional[str] = None
    subtitle: Optional[str] = None
    description: Optional[str] = None


class CartesianView(BaseView):
    x: str
    y: Union[str, List[str]]
    groupBy: Optional[str] = None
    stacked: bool = False
    smooth: bool = False
    axisX: AxisOptions = Field(
        default_factory=lambda: AxisOptions(rotate=45, interval=0)
    )
    axisY: AxisOptions = Field(
        default_factory=lambda: AxisOptions(show=True, rotate=0, interval=0)
    )
    legend: LegendOptions = Field(default_factory=LegendOptions)
    palette: Palette = Field(default_factory=Palette)
    seriesStyles: List[SeriesStyle] = Field(default_factory=list)
    showGrid: bool = True
    showDataZoom: bool = False


class BarChartView(CartesianView):
    type: Literal["bar_chart"] = "bar_chart"
    horizontal: bool = False
    barWidth: Optional[int] = None


class LineChartView(CartesianView):
    type: Literal["line_chart"] = "line_chart"
    smooth: bool = True


class AreaChartView(CartesianView):
    type: Literal["area_chart"] = "area_chart"
    smooth: bool = True
    area: bool = True
    opacity: float = 0.35


class PieChartView(BaseView):
    type: Literal["pie_chart"] = "pie_chart"
    category: str
    value: str
    donut: bool = False
    innerRadius: float = 0.6
    palette: Palette = Field(default_factory=Palette)
    showLabels: bool = True
    legend: LegendOptions = Field(default_factory=lambda: LegendOptions(position="right"))


class ScatterPlotView(BaseView):
    type: Literal["scatter_plot"] = "scatter_plot"
    x: str
    y: str
    size: Optional[str] = None
    colorBy: Optional[str] = None
    palette: Palette = Field(default_factory=Palette)
    axisX: AxisOptions = Field(default_factory=lambda: AxisOptions(rotate=0))
    axisY: AxisOptions = Field(default_factory=lambda: AxisOptions(rotate=0))


class HeatmapView(BaseView):
    type: Literal["heatmap"] = "heatmap"
    x: str
    y: str
    value: str
    colorScheme: Literal["blue", "green", "red", "violet", "orange"] = "blue"
    showValues: bool = True
    axisX: AxisOptions = Field(default_factory=lambda: AxisOptions(rotate=45))
    axisY: AxisOptions = Field(default_factory=lambda: AxisOptions(rotate=0))


class CountView(BaseView):
    type: Literal["count"] = "count"
    value: Optional[str] = None
    format: Literal["number", "currency", "percent", "compact"] = "number"
    prefix: Optional[str] = None
    suffix: Optional[str] = None
    palette: Palette = Field(default_factory=Palette)


class MetricCardView(BaseView):
    type: Literal["metric_card"] = "metric_card"
    value: str
    comparison: Optional[str] = None
    format: Literal["number", "currency", "percent", "compact"] = "number"
    prefix: Optional[str] = None
    suffix: Optional[str] = None
    trendIndicator: Literal["arrow", "sparkline", "none"] = "arrow"
    trendDirection: Optional[Literal["up", "down", "flat"]] = None
    palette: Palette = Field(default_factory=Palette)


class TableView(BaseView):
    type: Literal["table"] = "table"
    columns: Optional[List[str]] = None
    sortBy: Optional[str] = None
    sortOrder: Literal["asc", "desc"] = "asc"
    pageSize: int = 50


ChartView = Annotated[
    Union[
        BarChartView,
        LineChartView,
        AreaChartView,
        PieChartView,
        ScatterPlotView,
        HeatmapView,
        CountView,
        MetricCardView,
        TableView,
    ],
    Field(discriminator="type"),
]


class ViewSchema(BaseModel):
    """
    ViewSchema v2 - supports both new structured views and legacy formats.
    
    When `view` is None, the schema acts as a passthrough for legacy data.
    """
    version: Literal["v2", "legacy"] = "v2"
    view: Optional[ChartView] = None
    legacy: Optional[Dict[str, Any]] = None
    
    # Legacy fields for backward compatibility (flatten into root)
    type: Optional[str] = None
    encoding: Optional[Dict[str, Any]] = None
    options: Optional[Dict[str, Any]] = None
    
    class Config:
        extra = "allow"
    
    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["ViewSchema"]:
        """Safely construct ViewSchema from arbitrary dict, returning None if invalid."""
        if not data or not isinstance(data, dict):
            return None
        try:
            return cls.model_validate(data)
        except Exception:
            # Return a legacy wrapper for old format data
            return cls(version="legacy", legacy=data, type=data.get("type"))


VISUALIZATION_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "table": {"axes": False, "legend": False, "grid": False, "labels": False},
    "count": {"axes": False, "legend": False, "grid": False, "labels": True},
    "metric_card": {"axes": False, "legend": False, "grid": False, "labels": True},
    "bar_chart": {"axes": True, "legend": True, "grid": True, "labels": True},
    "line_chart": {"axes": True, "legend": True, "grid": True, "labels": True},
    "area_chart": {"axes": True, "legend": True, "grid": True, "labels": True},
    "pie_chart": {"axes": False, "legend": True, "grid": False, "labels": True},
    "scatter_plot": {"axes": True, "legend": False, "grid": False, "labels": True},
    "heatmap": {"axes": True, "legend": False, "grid": False, "labels": True},
}


def visualization_metadata() -> Dict[str, Any]:
    return {
        "capabilities": VISUALIZATION_CAPABILITIES,
        "palettes": SHADCN_THEMES,
    }


# -----------------------------------------------------------------------------
# Legacy exports for backward compatibility
# These are deprecated and will be removed in a future version
# -----------------------------------------------------------------------------

class SeriesEncodingSchema(BaseModel):
    """Legacy: per-series encoding (deprecated, use SeriesStyle)."""
    name: Optional[str] = None
    value: Optional[str] = None
    key: Optional[str] = None

    class Config:
        extra = "allow"


class EncodingSchema(BaseModel):
    """Legacy: data-to-visual mapping (deprecated, use view.view directly)."""
    category: Optional[str] = None
    value: Optional[str] = None
    series: Optional[List[SeriesEncodingSchema]] = None
    x: Optional[str] = None
    y: Optional[str] = None
    color: Optional[str] = None
    size: Optional[str] = None
    time: Optional[str] = None
    open: Optional[str] = None
    high: Optional[str] = None
    low: Optional[str] = None
    close: Optional[str] = None
    path: Optional[List[str]] = None
    id: Optional[str] = None
    parentId: Optional[str] = None
    name: Optional[str] = None
    dimensions: Optional[List[str]] = None

    class Config:
        extra = "allow"


