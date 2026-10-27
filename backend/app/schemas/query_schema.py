from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from app.schemas.visualization_schema import VisualizationSchema, PublicVisualizationSchema
from app.schemas.step_schema import StepSchema, PublicStepSchema


class QueryCreate(BaseModel):
    title: str
    description: Optional[str] = None
    report_id: Optional[str] = None
    widget_id: Optional[str] = None
    organization_id: Optional[str] = None
    user_id: Optional[str] = None


class QueryStepStatusSchema(BaseModel):
    """One query's default-step state, without rows: what a dashboard polls
    while its copied queries run again for the person who brought it."""
    query_id: str
    title: Optional[str] = None
    step_id: Optional[str] = None
    status: Optional[str] = None
    # Only for the conversation's owner -- the person whose run it was.
    status_reason: Optional[str] = None
    error_code: Optional[str] = None


class QueryLastRunSchema(BaseModel):
    """The most recent executed step (success OR error) for a query.

    Distinct from `default_step`, which only repoints on success and so
    hides a failing latest run behind the last good one.
    """
    status: str
    status_reason: Optional[str] = None
    ran_at: Optional[datetime] = None


class QuerySchema(BaseModel):
    source_refs: Optional[list] = None
    id: str
    title: str
    report_id: Optional[str] = None
    widget_id: str
    organization_id: Optional[str] = None
    user_id: Optional[str] = None
    default_step_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    visualizations: Optional[list[VisualizationSchema]] = None
    default_step: Optional[StepSchema] = None
    # Declared ParamSpec dicts (see app/schemas/param_schema.py)
    parameters: Optional[list] = None
    # True when this query's report reads a user-scoped (delegated) connection:
    # runs authenticate per user, so a View-as preview swaps identity params
    # only — source-level rows still come back under the caller's credentials.
    credential_scoped: Optional[bool] = None
    # Populated by the queries routes (not from_attributes): latest executed
    # step regardless of outcome, for "last run" status displays.
    last_run: Optional[QueryLastRunSchema] = None

    class Config:
        from_attributes = True


class QueryRunRequest(BaseModel):
    # Builder mode ('builder', default): `code` is required — creates a new
    # Step, executes it, repoints default_step_id. May also update the query's
    # declared `parameters`.
    # Viewer mode ('viewer'): `code` is ignored — executes the query's default
    # step code with `params`, caches the result per (step, viewer, values),
    # creates NO new Step.
    code: Optional[str] = None
    title: Optional[str] = None
    data_model: Optional[dict] = None
    type: Optional[str] = None
    row_limit: Optional[int] = None
    tool_execution_id: Optional[str] = None
    mode: Optional[str] = "builder"
    # Param values for this run ({name: value})
    params: Optional[dict] = None
    # Builder mode only: replace the query's declared ParamSpec list
    parameters: Optional[list] = None
    # Viewer mode only: bypass the cached per-viewer result
    force_refresh: bool = False
    # View-as (owner/admin only, server-verified): resolve identity params as
    # this org member instead of the caller. Results are preview-only.
    run_as_user_id: Optional[str] = None


class PublicQuerySchema(BaseModel):
    """Minimal schema for public/unauthenticated access to published reports."""
    id: str
    title: str
    default_step_id: Optional[str] = None
    visualizations: Optional[List[PublicVisualizationSchema]] = None
    # Declared ParamSpec dicts — needed so shared dashboards can render
    # controls; identity params are resolved server-side on /run regardless.
    parameters: Optional[list] = None

    class Config:
        from_attributes = True


class PublicQueryWithStepSchema(PublicQuerySchema):
    """A shared report's query plus the default step this reader may see.

    `default_step` is exactly what GET /r/{report_id}/queries/{id}/step returns
    for the same reader, or None where that route would 404.
    """
    default_step: Optional[PublicStepSchema] = None


