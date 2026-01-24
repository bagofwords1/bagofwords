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

    # Echoed input for UI hydration (can be a list or string)
    search_query: Optional[Union[str, List[str]]] = Field(default=None, description="Original query echoed for UI display")

    # Lightweight preview for UI: top tables across data sources
    class TableColumnPreview(BaseModel):
        name: str
        dtype: Optional[str] = None

    class TableUsagePreview(BaseModel):
        usage_count: Optional[int] = None
        success_count: Optional[int] = None
        failure_count: Optional[int] = None
        success_rate: Optional[float] = None
        last_used_at: Optional[str] = None
        score: Optional[float] = None

    class TablePreview(BaseModel):
        data_source_id: Optional[str] = None
        data_source_name: Optional[str] = None
        data_source_type: Optional[str] = None
        schema: Optional[str] = None  # optional; not all backends provide this
        name: str
        full_name: Optional[str] = None
        description: Optional[str] = None
        columns: List["DescribeTablesOutput.TableColumnPreview"] = Field(default_factory=list)
        usage: Optional["DescribeTablesOutput.TableUsagePreview"] = None

    top_tables: List[TablePreview] = Field(default_factory=list, description="Flattened sample of top tables with basic columns and usage")

    # Related instructions loaded via table references (only for load_mode='intelligent')
    class InstructionPreview(BaseModel):
        id: str
        title: Optional[str] = None
        category: Optional[str] = None
        text: Optional[str] = None
        load_mode: Optional[str] = None

    related_instructions: List[InstructionPreview] = Field(default_factory=list, description="Instructions referencing the matched tables (intelligent load_mode only)")


