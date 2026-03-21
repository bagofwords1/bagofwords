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

    Returns results for each requested query/visualization.
    """

    success: bool = Field(..., description="Whether all lookups succeeded")
    results: List[ReadQueryResult] = Field(default_factory=list, description="Results for each query/visualization")
    errors: Optional[List[str]] = Field(default=None, description="Global errors if the entire operation failed")
