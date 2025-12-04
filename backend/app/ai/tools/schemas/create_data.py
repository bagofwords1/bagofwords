from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field

# Reuse per-source targeting schema for consistent behavior
from .create_widget import TablesBySource
from .create_data_model import DataModel


class CreateDataInput(BaseModel):
    """Input for code-first data creation (no explicit data model).

    Generates and executes code to produce tabular data based on the prompt and targeted schemas.
    """

    title: str = Field(..., description="Title for the data artifact to create")
    user_prompt: str = Field(..., description="Original user instruction")
    interpreted_prompt: str = Field(..., description="LLM-interpreted, clarified version of the user prompt")

    tables_by_source: Optional[List[TablesBySource]] = Field(
        default=None,
        description=(
            "Compact per-source table targeting: [{data_source_id, tables:[...]}, ...]. "
            "Avoids repeating ds_id per table and supports cross-source patterns when data_source_id is null."
            "For file analysis only, keep this empty."
        ),
    )
    visualization_type: Optional[str] = Field(
        default=None,
        description="Type of visualization to create. If not provided, a table will be created.",
        choices=[
            "table",
            "bar_chart",
            "line_chart",
            "pie_chart",
            "area_chart",
            "count",
            "metric_card",  # KPI card with optional sparkline
            "heatmap",
            "map",
            "candlestick",
            "treemap",
            "radar_chart",
            "scatter_plot",
        ]
    )


class CreateDataOutput(BaseModel):
    """Output of code-first data creation."""

    success: bool = Field(..., description="Whether the overall operation succeeded")
    code: Optional[str] = Field(default=None, description="Final code used to compute data")
    data: Optional[Dict[str, Any]] = Field(default=None, description="Rendered tabular data structure for consumers")
    data_preview: Optional[Dict[str, Any]] = Field(default=None, description="Privacy-safe preview for UI/LLM")
    stats: Optional[Dict[str, Any]] = Field(default=None, description="Execution stats/metadata")
    execution_log: Optional[str] = Field(default=None, description="Execution log or trace output if available")
    errors: Optional[List[Any]] = Field(default=None, description="Internal retry errors, if any")
    # Minimal data model (no columns) for visualization: type + optional series/grouping
    data_model: Optional[DataModel] = Field(default=None, description="Minimal data model: type + optional series/group_by/sort/limit; columns omitted")
    # Optional view options (styling/palette) to merge into Visualization.view.options
    view_options: Optional[Dict[str, Any]] = Field(default=None, description="Optional view.options overrides, e.g., colors palette")


