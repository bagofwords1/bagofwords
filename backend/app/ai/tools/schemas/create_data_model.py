from typing import Dict, Any, List, Optional, Literal, Union
from pydantic import BaseModel, Field


class CreateDataModelInput(BaseModel):
    """Input for creating a data model from a high-level goal.

    AgentV2 will create/persist the widget/step. This tool only returns the data_model.
    """

    widget_title: str = Field(..., description="Title for the widget to be created by the agent")
    prompt: str = Field(..., description="User prompt")


class DataModelColumn(BaseModel):
    generated_column_name: str = Field(..., description="Logical name for the generated column")
    source: str = Field(..., description="Source reference, e.g. datasource.schema.table.column or expression")
    description: str = Field(..., description="Human-friendly description; end with '.' when possible")
    source_data_source_id: str = Field(
        ..., 
        description="UUID of the data source for lineage tracking; required for all columns"
    )


class SeriesBarLinePieArea(BaseModel):
    name: str
    key: str
    value: str


class SeriesCandlestick(BaseModel):
    name: str
    key: str
    open: str
    close: str
    low: str
    high: str


class SeriesHeatmap(BaseModel):
    name: str
    x: str
    y: str
    value: str


class SeriesScatter(BaseModel):
    name: str
    x: str
    y: str
    size: Optional[str] = None


class SeriesMap(BaseModel):
    name: str
    key: str
    value: str


class SeriesTreemap(BaseModel):
    name: str
    id: str
    parentId: str
    value: str
    key: Optional[str] = None


class SeriesRadar(BaseModel):
    name: Optional[str] = None
    key: Optional[str] = None
    value: Optional[str] = None
    dimensions: Optional[List[str]] = None


SeriesItem = Union[
    SeriesBarLinePieArea,
    SeriesCandlestick,
    SeriesHeatmap,
    SeriesScatter,
    SeriesMap,
    SeriesTreemap,
    SeriesRadar,
]


class SortSpec(BaseModel):
    field: str
    direction: Literal["asc", "desc"] = "asc"


class DataModel(BaseModel):
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
    columns: List[DataModelColumn] = Field(default_factory=list)
    #filters: Optional[List[Dict[str, Any]]] = Field(default=None, description="Filter predicates")
    group_by: Optional[List[str]] = Field(default=None, description="Group-by fields")
    #sort: Optional[List[SortSpec]] = Field(default=None, description="Sorting specifications")
    #limit: Optional[int] = Field(default=100, description="Row limit")
    series: Optional[List[SeriesItem]] = Field(default=None, description="Chart series configuration if applicable")


class CreateDataModelOutput(BaseModel):
    """Output data model for downstream code generation and execution."""

    data_model: DataModel = Field(..., description="Normalized data model ready for code generation")
    widget_title: str = Field(..., description="Echo of the requested widget title")
