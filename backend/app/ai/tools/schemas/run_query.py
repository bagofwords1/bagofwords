from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class RunQueryInput(BaseModel):
    """Input for run_query tool.

    Executes an EXISTING query's saved code with caller-supplied parameter
    VALUES. Single target: "give me this query's result for these inputs".
    """

    query_id: Optional[str] = Field(
        default=None,
        description=(
            "Id of the existing query to run. Found as 'query_id' in previous "
            "create_data results, or on a <query> in the report context. "
            "A visualization id ('viz_id') is also accepted here and resolves "
            "to the query behind it."
        ),
    )
    visualization_id: Optional[str] = Field(
        default=None,
        description=(
            "Alternative handle for the same thing: the 'viz_id' from a previous "
            "create_data result. Resolves to that visualization's query. Pass "
            "either this or query_id, not both."
        ),
    )
    params: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Parameter VALUES ({name: value}) using the names declared on the "
            "query — see its <parameters> in the report context, or call "
            "read_query first. The saved code runs with these values; no code "
            "is generated. Omit a parameter to use its default; omit `params` "
            "entirely to run with all defaults. Identity-sourced parameters "
            "are resolved from the viewer server-side and must not be set here."
        ),
    )
    force_refresh: bool = Field(
        default=False,
        description=(
            "Bypass the cached per-viewer result for these values and execute "
            "again. Leave false unless the underlying data is expected to have "
            "changed within this conversation."
        ),
    )


class MissingParam(BaseModel):
    """A declared parameter the run needs a value for."""

    name: str = Field(..., description="Parameter name to supply")
    type: Optional[str] = Field(None, description="Value type: string|number|date|date_range|id|list")
    label: Optional[str] = Field(None, description="Human label for the parameter")
    description: Optional[str] = Field(None, description="Declared description, if any")
    required: bool = Field(False, description="Whether a value is mandatory")
    options: Optional[List[Any]] = Field(None, description="Allowed values, when declared statically")
    options_source: Optional[Dict[str, Any]] = Field(
        None, description="Query-backed option list: {query_id, value_column, label_column}"
    )


class RunQueryOutput(BaseModel):
    """Output from run_query tool."""

    success: bool = Field(..., description="Whether the run produced data")
    query_id: Optional[str] = Field(None, description="Query that was run")
    visualization_id: Optional[str] = Field(None, description="Visualization bound to that query, if any")
    step_id: Optional[str] = Field(None, description="Step whose saved code was executed")
    title: Optional[str] = Field(None, description="Query title")
    data: Optional[Dict[str, Any]] = Field(None, description="Tabular result of THIS run (columns + rows)")
    data_preview: Optional[Dict[str, Any]] = Field(None, description="Privacy-safe preview of this run's rows")
    data_model: Optional[Dict[str, Any]] = Field(None, description="Data model (chart type, series, group_by)")
    view: Optional[Dict[str, Any]] = Field(None, description="Visualization view config")
    applied_params: Optional[Dict[str, Any]] = Field(
        None, description="Resolved values THIS run executed with (defaults + supplied + identity)"
    )
    parameters: Optional[List[Dict[str, Any]]] = Field(
        None, description="Declared ParamSpec dicts on the query"
    )
    missing_params: Optional[List[MissingParam]] = Field(
        None,
        description=(
            "Parameters that still need values before this query can run. "
            "Ask the user for these (clarify) rather than guessing."
        ),
    )
    cached: bool = Field(False, description="Whether the result came from the per-viewer cache")
    error: Optional[str] = Field(None, description="Error message if the run failed")
