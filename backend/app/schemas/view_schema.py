from pydantic import BaseModel, Field, model_validator
from typing import Optional, Dict, Any, List, Literal


# Central capabilities registry for visualization types
# This serves both validation/sanitization and a lightweight metadata contract for the UI
VISUALIZATION_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    # Table is rendered elsewhere; treat as data-only
    "table": {
        "axes": False,
        "legend": False,
        "grid": False,
        "labels": False,
        "encodings": [],
    },
    # KPI/Count card
    "count": {
        "axes": False,
        "legend": False,
        "grid": False,
        "labels": True,  # allow title visibility toggle
        "encodings": ["value"],
    },
    # Cartesian families share the same capabilities
    "bar_chart": {
        "axes": True,
        "legend": True,
        "grid": True,
        "labels": True,
        "encodings": ["category", "value", "series"],
    },
    "line_chart": {
        "axes": True,
        "legend": True,
        "grid": True,
        "labels": True,
        "encodings": ["category", "value", "series"],
    },
    "area_chart": {
        "axes": True,
        "legend": True,
        "grid": True,
        "labels": True,
        "encodings": ["category", "value", "series"],
    },
    "scatter_plot": {
        "axes": True,
        "legend": False,
        "grid": False,
        "labels": True,
        "encodings": ["x", "y", "color", "size"],
    },
    "heatmap": {
        "axes": True,
        "legend": False,
        "grid": False,
        "labels": True,
        "encodings": ["x", "y", "value"],
    },
    "candlestick": {
        "axes": True,
        "legend": False,
        "grid": False,
        "labels": True,
        "encodings": ["time", "open", "high", "low", "close", "key"],
    },
    "treemap": {
        "axes": False,
        "legend": False,
        "grid": False,
        "labels": True,
        "encodings": ["id", "parentId", "name", "value", "path"],
    },
    "radar_chart": {
        "axes": False,
        "legend": True,
        "grid": False,
        "labels": True,
        "encodings": ["dimensions", "key", "name", "value"],
    },
}


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
    titleVisible: Optional[bool] = None       # Show/hide chart title
    
    # X-axis label display controls for categorical data
    xAxisLabelInterval: Optional[int] = 0  # default: show all labels
    xAxisLabelRotate: Optional[int] = 45   # default: rotate 45 degrees

    # Grid / background guides
    showGridLines: Optional[bool] = None

    class Config:
        extra = "allow"

    @property
    def capabilities(self) -> Dict[str, Any]:
        t = (self.type or "").lower()
        return VISUALIZATION_CAPABILITIES.get(t, {
            "axes": False,
            "legend": False,
            "grid": False,
            "labels": True,
            "encodings": [],
        })

    @model_validator(mode="after")
    def _sanitize_by_capabilities(self) -> "ViewSchema":
        """Drop or normalize fields that are not applicable for the given type.

        This keeps persisted views clean and prevents UI from reading irrelevant flags.
        """
        caps = self.capabilities

        # Hide axes/grid flags if unsupported
        if not caps.get("axes"):
            self.xAxisVisible = None
            self.yAxisVisible = None
            # Also remove label controls if present
            self.xAxisLabelInterval = None
            self.xAxisLabelRotate = None
        if not caps.get("grid"):
            self.showGridLines = None
        if not caps.get("legend") and self.legendVisible is not None:
            # Force None so model_dump(exclude_none=True) drops it
            self.legendVisible = None

        # Encoding pruning
        if self.encoding is not None:
            allowed = set(caps.get("encodings", []))
            if not allowed:
                # No encodings for this type
                self.encoding = None
            else:
                self.encoding = self.encoding.pruned(allowed)
        return self



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

    def pruned(self, allowed: set[str]) -> "EncodingSchema":
        """Return a copy with only allowed keys retained in the top-level encoding
        and with per-series keys preserved but pruned as appropriate.
        """
        data: Dict[str, Any] = self.model_dump()
        out: Dict[str, Any] = {}
        # Keep only allowed top-level fields
        for k, v in data.items():
            if k == "series" and isinstance(v, list):
                # For series entries, keep name/value plus any allowed keys
                pruned_series: List[Dict[str, Any]] = []
                for s in v:
                    if not isinstance(s, dict):
                        continue
                    keep: Dict[str, Any] = {}
                    for sk, sv in s.items():
                        if sk in allowed or sk in {"name", "value"}:
                            keep[sk] = sv
                    if keep:
                        pruned_series.append(keep)
                if pruned_series:
                    out["series"] = pruned_series
            elif k in allowed:
                out[k] = v
        # Reconstruct as EncodingSchema
        try:
            return EncodingSchema(**out)
        except Exception:
            # If reconstruction fails, drop encoding entirely to avoid bad state
            return EncodingSchema()


def visualization_metadata() -> Dict[str, Any]:
    """Expose a minimal capabilities descriptor for the UI.

    Returned shape:
    { type: { axes, legend, grid, labels, encodings: [...] } }
    """
    return VISUALIZATION_CAPABILITIES

