from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class ReadQueryInput(BaseModel):
    """Input for read_query tool.

    Looks up previously created queries/visualizations from the current report.
    Accepts one or more query_ids and/or visualization_ids.

    SLICE MODE (Investigation Artifact Store): when a previous create_data
    result was large, its observation includes an `artifact` block with an
    `artifact_id` — the FULL result was retained as a sliceable artifact.
    Pass `artifact_id` (or a query_id whose step has an artifact) together
    with any of offset/limit (page), match (regex grep), columns (projection),
    time_from/time_to (time window), or sql (SELECT-only over table `data`)
    to explore the full result beyond the stored preview. Results are always
    bounded pages with a `next_offset` cursor.
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

    # --- slice mode (artifact store) ---
    artifact_id: Optional[str] = Field(
        default=None,
        description="Artifact ID from a previous create_data observation's 'artifact' block. Enables slice mode over the FULL retained result.",
    )
    offset: Optional[int] = Field(default=None, description="Slice mode: row offset for paging (default 0).")
    limit: Optional[int] = Field(default=None, description="Slice mode: max rows per page (bounded server-side).")
    match: Optional[str] = Field(default=None, description="Slice mode: regex to grep rows (RE2-style syntax; matched against all columns unless match_column is set).")
    match_column: Optional[str] = Field(default=None, description="Slice mode: restrict the regex match to one column.")
    columns: Optional[List[str]] = Field(default=None, description="Slice mode: project only these columns.")
    time_from: Optional[str] = Field(default=None, description="Slice mode: ISO timestamp lower bound (requires the artifact to have a timestamp column).")
    time_to: Optional[str] = Field(default=None, description="Slice mode: ISO timestamp upper bound.")
    sql: Optional[str] = Field(
        default=None,
        description="Slice mode: a single SELECT statement over table `data` (aggregations, GROUP BY, etc.). Read-only; DDL/DML/file access are rejected.",
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
    error: Optional[str] = Field(None, description="Error message if this lookup failed")


class ReadQueryOutput(BaseModel):
    """Output from read_query tool.

    Returns results for each requested query/visualization, or a slice of an
    artifact when slice mode is used.
    """

    success: bool = Field(..., description="Whether all lookups succeeded")
    results: List[ReadQueryResult] = Field(default_factory=list, description="Results for each query/visualization")
    slice: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Slice-mode result: columns, rows (bounded page), total_matches, offset, limit, next_offset.",
    )
    errors: Optional[List[str]] = Field(default=None, description="Global errors if the entire operation failed")
