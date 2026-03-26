from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field

from .create_data_model import DataModel


class TablesBySource(BaseModel):
    """Per-source table filters to focus schema loading.

    - data_source_id: scope to a specific data source (UUID). If omitted/null, applies across all sources.
    - connection_id: scope to a specific connection (UUID). Useful when a data source has multiple
      connections with similar or identically-named tables. If omitted/null, matches across all connections.
    - tables: list of table names. Names are always treated as literal (escaped).
      Matching is case-insensitive and names match with or without a schema/dataset prefix (. or / separator).
      Examples: "film", "public.inventory", "Regional Sales Sample (2)/Opportunities".
    """

    data_source_id: Optional[str] = Field(
        default=None,
        description="UUID of the data source to scope these tables. If null, applies to all sources.",
    )
    connection_id: Optional[str] = Field(
        default=None,
        description="UUID of a specific connection to scope these tables. Use when a data source has multiple connections with similar table names. If null, matches across all connections.",
    )
    tables: List[str] = Field(
        ..., description="Table names (literal, case-insensitive). Schema or dataset prefix (. or /) is optional."
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
            "Compact per-source table targeting: [{data_source_id, connection_id, tables:[...]}, ...]. "
            "Avoids repeating ds_id per table and supports cross-source patterns when data_source_id is null. "
            "Use connection_id to disambiguate when multiple connections have similar table names."
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


