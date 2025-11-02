from typing import List, Optional, Union
from pydantic import BaseModel, Field


class DescribeTablesInput(BaseModel):
    """Minimal input for describing tables from the schema index.

    - query: table names or simple patterns; regex is auto-detected by special chars
    - data_source_ids: optional scope of data sources (UUID strings)
    - limit: soft cap for how many tables to include in the rendered excerpt per data source
    """

    query: Union[str, List[str]] = Field(..., description="Table names or patterns")
    data_source_ids: Optional[List[str]] = Field(
        default=None, description="Optional list of data source IDs (UUIDs) to scope search"
    )
    limit: int = Field(10, ge=1, le=100, description="Max tables to sample per data source in excerpt")


class DescribeTablesOutput(BaseModel):
    """Planner-ready excerpt and light telemetry about the describe operation."""

    schemas_excerpt: str = Field(..., description="Schemas XML excerpt identical to the main schemas section format")
    truncated: bool = Field(False, description="True if results were truncated by the provided limit")
    searched_sources: int = Field(..., description="Number of data sources examined")
    searched_tables_est: int = Field(..., description="Estimated total number of matched tables across sources")
    errors: List[str] = Field(default_factory=list, description="Non-fatal errors encountered while rendering")


