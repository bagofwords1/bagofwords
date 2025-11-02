from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field

from .create_data_model import DataModel


class TablesBySource(BaseModel):
    """Per-source table filters to focus schema loading.

    - data_source_id: scope to a specific data source (UUID). If omitted/null, applies across all sources.
    - tables: list of table identifiers or lightweight patterns. Items are interpreted as:
        * literal names (e.g., "film", "public.inventory").
        * regex patterns if they contain special chars (e.g., ".*_list$").
      Matching is case-insensitive and simple names match with or without a schema prefix.
    """

    data_source_id: Optional[str] = Field(
        default=None,
        description="UUID of the data source to scope these tables. If null, applies to all sources.",
    )
    tables: List[str] = Field(
        ..., description="Table names or patterns (regex allowed). Case-insensitive; schema prefix optional."
    )


class CreateWidgetInput(BaseModel):
    """Input for end-to-end widget creation.

    The tool will generate a data_model, then code, then execute it to populate the widget.
    """

    widget_title: str = Field(..., description="Title for the widget to create")
    user_prompt: str = Field(..., description="Original user instruction")
    interpreted_prompt: str = Field(..., description="LLM-interpreted, clarified version of the user prompt")

    tables_by_source: Optional[List[TablesBySource]] = Field(
        default=None,
        description=(
            "Compact per-source table targeting: [{data_source_id, tables:[...]}, ...]. "
            "Avoids repeating ds_id per table and supports cross-source patterns when data_source_id is null."
        ),
    )
    schema_limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Max tables to include per data source when rendering the schema excerpt.",
    )


class CreateWidgetOutput(BaseModel):
    """Output of end-to-end widget creation."""

    success: bool = Field(..., description="Whether the overall operation succeeded")
    data_model: Optional[DataModel] = Field(default=None, description="Final normalized data model")
    code: Optional[str] = Field(default=None, description="Final code used to compute widget data")
    widget_data: Optional[Dict[str, Any]] = Field(default=None, description="Rendered data structure for the widget")
    data_preview: Optional[Dict[str, Any]] = Field(default=None, description="Privacy-safe preview for UI/LLM")
    stats: Optional[Dict[str, Any]] = Field(default=None, description="Execution stats/metadata")
    execution_log: Optional[str] = Field(default=None, description="Execution log or trace output if available")


