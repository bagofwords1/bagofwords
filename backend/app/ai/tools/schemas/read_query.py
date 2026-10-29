from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class ReadQueryInput(BaseModel):
    """Input for read_query tool.

    Looks up previously created queries/visualizations from the current report.
    Accepts one or more query_ids and/or visualization_ids.
    """

    query_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "List of query IDs to read. "
            "Found in previous create_data results as 'query_id' in the conversation history."
        ),
    )
    visualization_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "List of visualization IDs to read. "
            "Found in previous create_data results as 'viz_id' in the conversation history."
        ),
    )
    offset: Optional[int] = Field(
        default=None, ge=0,
        description=(
            "Read rows page by page: 0-based index of the first row to return. Use it (with limit) "
            "when the preview is truncated or you need rows beyond it; continue from the returned "
            "page.next_offset until page.eof. Rows past the saved snapshot are fetched by re-running "
            "the query (queries in this report only)."
        ),
    )
    limit: Optional[int] = Field(
        default=None, ge=1, le=500,
        description="Rows per page when paging (default 100, max 500; also capped by the org row limit and a size budget).",
    )


class ReadQueryResult(BaseModel):
    """Result for a single query/visualization lookup."""

    query_id: Optional[str] = Field(None, description="Query ID")
    visualization_id: Optional[str] = Field(None, description="Visualization ID")
    title: Optional[str] = Field(None, description="Query title")
    code: Optional[str] = Field(None, description="Code used to generate the data")
    data: Optional[Dict[str, Any]] = Field(None, description="Stored tabular data (columns + rows)")
    data_preview: Optional[Dict[str, Any]] = Field(None, description="Privacy-safe data preview")
    data_model: Optional[Dict[str, Any]] = Field(None, description="Data model (chart type, series, group_by)")
    view: Optional[Dict[str, Any]] = Field(None, description="Visualization view config")
    step_id: Optional[str] = Field(None, description="Step ID")
    parameters: Optional[List[Dict[str, Any]]] = Field(
        None, description="Declared ParamSpec dicts on the query (server-side parameters)"
    )
    applied_params: Optional[Dict[str, Any]] = Field(
        None, description="Param values the step's snapshot was produced with"
    )
    page: Optional[Dict[str, Any]] = Field(
        None,
        description="Rows window when offset/limit were given: {offset, limit, returned, total_rows, next_offset, eof, source, columns, rows}",
    )
    error: Optional[str] = Field(None, description="Error message if this lookup failed")


class ReadQueryOutput(BaseModel):
    """Output from read_query tool.

    Returns results for each requested query/visualization.
    """

    success: bool = Field(..., description="Whether all lookups succeeded")
    results: List[ReadQueryResult] = Field(default_factory=list, description="Results for each query/visualization")
    errors: Optional[List[str]] = Field(default=None, description="Global errors if the entire operation failed")
