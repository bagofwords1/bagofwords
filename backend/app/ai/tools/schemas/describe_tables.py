from typing import List, Optional, Union
from pydantic import BaseModel, Field, model_validator


class DescribeTablesInput(BaseModel):
    """Minimal input for describing tables from the schema index.

    - query: table names or simple patterns; regex is auto-detected by special chars
    - data_source_ids: optional scope of data sources (UUID strings)
    - connection_ids: optional scope to specific connections (UUID strings); use when the same table exists across multiple connections
    - limit: soft cap for how many tables to include in the rendered excerpt per data source
    """

    query: Optional[Union[str, List[str]]] = Field(
        default=None,
        description="Table, procedure or function names, or patterns. Omit it (and pass `columns`) to search columns across every table in scope.",
    )
    columns: Optional[Union[str, List[str]]] = Field(
        default=None,
        description=(
            "Column names or patterns to search for (matched against column name and description, case-insensitive; "
            "plain text matches as a substring, `*` as a wildcard, or a regex). With `query`: only the matching columns "
            "of those tables are returned. Without `query`: every table in scope that has a matching column is returned, "
            "with just those columns. Use it to find columns a wide table's schema summary folded away."
        ),
    )
    data_source_ids: Optional[List[str]] = Field(
        default=None, description="Optional list of data source IDs (UUIDs) to scope search"
    )
    connection_ids: Optional[List[str]] = Field(
        default=None, description="Optional list of connection IDs (UUIDs) to scope search. Use when the same table name exists across multiple connections to avoid duplicates."
    )
    limit: int = Field(10, ge=1, le=100, description="Max tables to sample per data source in excerpt")

    @model_validator(mode="after")
    def _query_or_columns(self):
        def _empty(v):
            return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, list) and not any(
                isinstance(x, str) and x.strip() for x in v))
        if _empty(self.query) and _empty(self.columns):
            raise ValueError("pass `query` (table names/patterns), `columns` (column names/patterns), or both")
        return self


class DescribeTablesOutput(BaseModel):
    """Planner-ready excerpt and light telemetry about the describe operation."""

    schemas_excerpt: str = Field(..., description="Schemas XML excerpt identical to the main schemas section format")
    truncated: bool = Field(False, description="True if results were truncated by the provided limit")
    searched_sources: int = Field(..., description="Number of data sources examined")
    searched_tables_est: int = Field(..., description="Estimated total number of matched tables across sources")
    errors: List[str] = Field(default_factory=list, description="Non-fatal errors encountered while rendering")

    # Echoed input for UI hydration (can be a list or string)
    search_query: Optional[Union[str, List[str]]] = Field(default=None, description="Original query echoed for UI display")
    search_columns: Optional[Union[str, List[str]]] = Field(default=None, description="Original column search echoed for UI display")

    # Lightweight preview for UI: top tables across data sources
    class TableColumnPreview(BaseModel):
        name: str
        dtype: Optional[str] = None
        description: Optional[str] = None
        metadata: Optional[dict] = None  # Column-level metadata (formula, role, __typename, etc.)
        usage_count: Optional[int] = None  # executed queries that used the column (ColumnStats)

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
        connection_name: Optional[str] = None
        connection_type: Optional[str] = None
        schema: Optional[str] = None  # optional; not all backends provide this
        name: str
        full_name: Optional[str] = None
        description: Optional[str] = None
        metadata: Optional[dict] = None  # Table-level metadata (tableau info, schema, etc.)
        columns: List["DescribeTablesOutput.TableColumnPreview"] = Field(default_factory=list)
        usage: Optional["DescribeTablesOutput.TableUsagePreview"] = None
        # `columns` is a bounded list: the matching columns of a column search,
        # or the top columns of a wide table. columns_total is the table's size.
        columns_total: Optional[int] = None
        columns_matched: Optional[int] = None

    top_tables: List[TablePreview] = Field(default_factory=list, description="Flattened sample of top tables with basic columns and usage")

    # Related instructions loaded via table references (only for load_mode='intelligent')
    class InstructionPreview(BaseModel):
        id: str
        title: Optional[str] = None
        category: Optional[str] = None
        text: Optional[str] = None
        load_mode: Optional[str] = None

    related_instructions: List[InstructionPreview] = Field(default_factory=list, description="Instructions referencing the matched tables (intelligent load_mode only)")


