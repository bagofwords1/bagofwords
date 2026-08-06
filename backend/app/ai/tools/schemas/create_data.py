from typing import Dict, Any, Optional, List, Literal
from pydantic import BaseModel, Field

# Reuse per-source targeting schema for consistent behavior
from .create_widget import TablesBySource
from .create_data_model import DataModel


VisualizationType = Literal[
    "table",
    "bar_chart",
    "line_chart",
    "pie_chart",
    "area_chart",
    "count",
    "metric_card",
    "heatmap",
    "map",
    "candlestick",
    "treemap",
    "radar_chart",
    "scatter_plot",
]


class CreateDataInput(BaseModel):
    """Input for code-first data creation (no explicit data model).

    Generates and executes code to produce tabular data based on the prompt and targeted schemas.
    """

    title: str = Field(..., description="Title for the data artifact to create. Should be in the same language of the user/prompt")
    user_prompt: str = Field(..., description="Original user instruction")
    interpreted_prompt: str = Field(..., description="LLM-interpreted, clarified version of the user prompt. "
    "Be prescriptive: name specific tables, target columns, and additional columns for filtering. "
    "Prefer a single wide master table with multiple metrics and dimensions over many narrow queries. "
    "Specify whether to return granular rows or pre-aggregate. "
    "Reuse additional columns from previous queries when the data is related, to enable cross-filtering. "
    "Time windows: express them RELATIVELY ('the latest day in the data', 'last 7 days at run time', "
    "'this month'), never resolved into literal dates — the generated code is re-executed verbatim on "
    "dashboard refresh and scheduled runs, so a resolved date goes permanently stale. Name a literal "
    "date only when the user explicitly fixed one.")

    tables_by_source: Optional[List[TablesBySource]] = Field(
        default=None,
        description=(
            "Compact per-source table targeting: [{data_source_id, tables:[...]}, ...]. "
            "Avoids repeating ds_id per table and supports cross-source patterns when data_source_id is null. "
            "EVERY create_data call must name its data source: set this (with a non-empty tables list) "
            "for any database query, or set source_file_ids instead when the data comes from files or "
            "tool results — set both when mixing. A call with neither is invalid and fails without "
            "producing data (the only exception is a pure URL-fetch task in a workspace with web fetch "
            "enabled). Never omit both just because tables were mentioned earlier in the conversation — "
            "restate them here on every call."
        ),
    )
    source_file_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "File IDs this code should read, e.g. the file_id returned by "
            "execute_mcp. Pass it whenever the data comes from a tool result or "
            "an attached file: it pins the generated code to those files and "
            "tells it the exact path, reader and column shape. A clean tabular "
            "MCP result needs only this — no write_csv step in between. "
            "This is the file alternative to tables_by_source: every call must "
            "set at least one of the two."
        ),
    )
    visualization_type: Optional[VisualizationType] = Field(
        default=None,
        description="Type of visualization to create. If not provided, a table will be created.",
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


