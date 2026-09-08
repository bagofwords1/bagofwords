from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db, get_current_organization, release_request_db
from app.services.console_service import ConsoleService
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.console_access import ConsoleScope, console_scope
from app.ee.license import require_enterprise
from app.schemas.console_schema import SimpleMetrics, MetricsQueryParams, MetricsComparison, TimeSeriesMetrics, TableUsageData, TableUsageMetrics, TableJoinsHeatmap, TableJoinData, ToolUsageMetrics, LLMUsageMetrics, CostMetrics
from typing import Optional, List, Dict
from datetime import datetime, timedelta
from app.models.step import Step
from app.models.widget import Widget
from app.models.report import Report
from sqlalchemy import select, func
from app.schemas.console_schema import DateRange
import logging
import re
from collections import Counter, defaultdict
import json
from app.schemas.console_schema import TopUsersMetrics, RecentNegativeFeedbackMetrics, TraceData, AgentExecutionSummariesResponse
from app.schemas.agent_execution_trace_schema import AgentExecutionTraceResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["console"])
console_service = ConsoleService()

# Access to every endpoint below is decided by `console_scope` (see
# app/core/console_access.py): org admins get the org-wide view, agent managers
# get the same console narrowed to the agents they manage. Each handler clamps
# its agent filter through `scope` before touching the service, so a scoped
# caller can never read past their grants.

@router.get("/console/metrics", response_model=SimpleMetrics)
async def get_console_metrics(
    params: MetricsQueryParams = Depends(),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    scope: ConsoleScope = Depends(console_scope)
):
    """Get console metrics with optional date filtering"""
    _result = await console_service.get_organization_metrics(db, organization, scope.scoped_params(params))
    await release_request_db(db)
    return _result

@router.get("/console/metrics/comparison", response_model=MetricsComparison)
async def get_console_metrics_comparison(
    params: MetricsQueryParams = Depends(),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    scope: ConsoleScope = Depends(console_scope)
):
    """Get console metrics with previous period comparison"""
    _result = await console_service.get_metrics_with_comparison(db, organization, scope.scoped_params(params))
    await release_request_db(db)
    return _result

@router.get("/console/recent-widgets")
async def get_recent_widgets(
    offset: int = 0,
    limit: int = 10,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    scope: ConsoleScope = Depends(console_scope)
):
    """Get recent widgets for the console with pagination"""
    _result = await console_service.get_recent_widgets(db, organization, current_user, offset, limit)
    await release_request_db(db)
    return _result

@router.get("/console/metrics/timeseries", response_model=TimeSeriesMetrics)
async def get_timeseries_metrics(
    params: MetricsQueryParams = Depends(),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    scope: ConsoleScope = Depends(console_scope)
):
    """Get time-series metrics data for charts"""
    _result = await console_service.get_timeseries_metrics(db, organization, scope.scoped_params(params))
    await release_request_db(db)
    return _result

@router.get("/console/metrics/table-usage", response_model=TableUsageMetrics)
async def get_table_usage(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    scope: ConsoleScope = Depends(console_scope)
):
    """Get table usage statistics"""
    _result = await console_service.get_table_usage_metrics(db, organization, scope.scoped_params(params))
    await release_request_db(db)
    return _result

@router.get("/console/metrics/table-joins-heatmap", response_model=TableJoinsHeatmap)
async def get_table_joins_heatmap(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    scope: ConsoleScope = Depends(console_scope)
):
    """Get table joins heatmap data"""
    _result = await console_service.get_table_joins_heatmap(db, organization, scope.scoped_params(params))
    await release_request_db(db)
    return _result

@router.get("/console/metrics/top-users", response_model=TopUsersMetrics)
async def get_top_users(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    scope: ConsoleScope = Depends(console_scope)
):
    """Get top users by activity with trend analysis"""
    _result = await console_service.get_top_users_metrics(db, organization, scope.scoped_params(params))
    await release_request_db(db)
    return _result

@router.get("/console/metrics/tool-usage", response_model=ToolUsageMetrics)
async def get_tool_usage(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    scope: ConsoleScope = Depends(console_scope)
):
    """Get tool usage counts for key tools."""
    _result = await console_service.get_tool_usage_metrics(db, organization, scope.scoped_params(params))
    await release_request_db(db)
    return _result

@router.get("/console/metrics/llm-usage", response_model=LLMUsageMetrics)
async def get_llm_usage(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    scope: ConsoleScope = Depends(console_scope)
):
    """Get aggregated LLM token/cost usage per model."""
    _result = await console_service.get_llm_usage_metrics(db, organization, scope.scoped_params(params))
    await release_request_db(db)
    return _result

@router.get("/console/metrics/cost", response_model=CostMetrics)
@require_enterprise(feature="cost_dashboard")
async def get_cost_metrics(
    group_by: str = "model",
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    scope: ConsoleScope = Depends(console_scope)
):
    """Get LLM cost/token spend broken down by a dimension (model, provider,
    user, data_source, group, scope) with a daily timeseries."""
    _result = await console_service.get_cost_metrics(db, organization, scope.scoped_params(params), group_by=group_by)
    await release_request_db(db)
    return _result

@router.get("/console/metrics/recent-negative-feedback", response_model=RecentNegativeFeedbackMetrics)
async def get_recent_negative_feedback(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    scope: ConsoleScope = Depends(console_scope)
):
    """Get recent negative feedback with completion context"""
    _result = await console_service.get_recent_negative_feedback_metrics(db, organization, scope.scoped_params(params))
    await release_request_db(db)
    return _result




@router.get("/console/trace/{report_id}/{completion_id}", response_model=TraceData)
async def get_trace_data(
    report_id: str,
    completion_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    scope: ConsoleScope = Depends(console_scope)
):
    """Get detailed trace data for debugging"""
    await scope.assert_report_visible(db, report_id)
    _result = await console_service.get_trace_data(db, organization, report_id, completion_id)
    await release_request_db(db)
    return _result

@router.get("/console/agent_executions/summaries", response_model=AgentExecutionSummariesResponse)
async def get_agent_execution_summaries(
    params: MetricsQueryParams = Depends(),
    page: int = 1,
    page_size: int = 20,
    filter: Optional[str] = None,
    tool_name: Optional[str] = None,
    prompt_search: Optional[str] = None,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    scope: ConsoleScope = Depends(console_scope)
):
    """Agent execution summaries joined with completion, feedback, and tool stats."""
    _result = await console_service.get_agent_execution_summaries(
        db, organization, scope.scoped_params(params), page, page_size, filter, tool_name, prompt_search
    )
    await release_request_db(db)
    return _result

@router.get("/console/diagnosis/metrics")
async def get_diagnosis_dashboard_metrics(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    scope: ConsoleScope = Depends(console_scope)
):
    """Get dashboard metrics for diagnosis page."""
    _result = await console_service.get_diagnosis_dashboard_metrics(db, organization, scope.scoped_params(params))
    await release_request_db(db)
    return _result


# ---------------------------------------------------------------------------
# Diagnosis explorer — one query language over agent runs and their tool calls
# (see app/services/diagnosis and docs/design/diagnosis-explorer.md)
# ---------------------------------------------------------------------------
from fastapi import HTTPException, Query  # noqa: E402
from app.services.diagnosis.service import (  # noqa: E402
    BadQuery, BadRequest, RunQueryParams, diagnosis_service,
)


def _diag_params(
    q: str = "",
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    tz: int = 0,
    cursor: Optional[str] = None,
    limit: int = 25,
    sort: str = "created",
    dir: str = "desc",
    include: Optional[str] = None,
) -> RunQueryParams:
    parts = {p.strip() for p in (include or "").split(",") if p.strip()}
    params = RunQueryParams(
        q=q or "", start=start, end=end, tz_offset_minutes=tz, cursor=cursor,
        limit=limit, sort=sort, sort_dir=dir,
    )
    if parts:
        params.include = parts
    return params


def _diag_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=getattr(exc, "detail", {"code": "bad_request", "message": str(exc)}))


@router.get("/console/diagnosis/runs")
async def diagnosis_runs(
    params: RunQueryParams = Depends(_diag_params),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    scope: ConsoleScope = Depends(console_scope),
):
    """A page of agent runs for a query, plus the summary line, the histogram
    and the per-tool strip (``include=items`` on cursor pages skips those)."""
    try:
        _result = await diagnosis_service.run_query(db, str(organization.id), scope.data_source_ids, params)
    except (BadQuery, BadRequest) as exc:
        raise _diag_error(exc)
    await release_request_db(db)
    return _result


@router.get("/console/diagnosis/runs/tool_calls")
async def diagnosis_tool_calls(
    run_ids: str = Query(..., description="Comma-separated run ids (≤100)"),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    scope: ConsoleScope = Depends(console_scope),
):
    """Tool calls for the runs on one page, keyed by run id. Runs outside the
    caller's scope are silently absent."""
    ids = [r.strip() for r in run_ids.split(",") if r.strip()]
    _result = await diagnosis_service.tool_calls(db, str(organization.id), scope.data_source_ids, ids)
    await release_request_db(db)
    return _result


@router.get("/console/diagnosis/facets/{field_name}")
async def diagnosis_facets(
    field_name: str,
    prefix: str = "",
    params: RunQueryParams = Depends(_diag_params),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    scope: ConsoleScope = Depends(console_scope),
):
    """Top values for a field within the time range and the rest of the query."""
    try:
        _result = await diagnosis_service.facets(db, str(organization.id), scope.data_source_ids, field_name, params, prefix)
    except (BadQuery, BadRequest) as exc:
        raise _diag_error(exc)
    await release_request_db(db)
    return _result


@router.get("/console/diagnosis/fields")
async def diagnosis_fields(
    current_user: User = Depends(current_user),
    scope: ConsoleScope = Depends(console_scope),
):
    """The field registry: powers the filter builder, suggestions and syntax help."""
    return diagnosis_service.fields()
