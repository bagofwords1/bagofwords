from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db, get_current_organization, release_request_db
from app.services.console_service import ConsoleService
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.ee.license import require_enterprise
from app.schemas.console_schema import SimpleMetrics, MetricsQueryParams, MetricsComparison, TimeSeriesMetrics, TableUsageData, TableUsageMetrics, TableJoinsHeatmap, TableJoinData, ToolUsageMetrics, LLMUsageMetrics, DiagnosisTimeSeriesMetrics, CostMetrics
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
from app.schemas.console_schema import TopUsersMetrics, RecentNegativeFeedbackMetrics, TraceData, CompactIssuesResponse, AgentExecutionSummariesResponse
from app.schemas.agent_execution_trace_schema import AgentExecutionTraceResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["console"])
console_service = ConsoleService()

@router.get("/console/metrics", response_model=SimpleMetrics)
@requires_permission('manage_settings')
async def get_console_metrics(
    params: MetricsQueryParams = Depends(),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """Get console metrics with optional date filtering"""
    _result = await console_service.get_organization_metrics(db, organization, params)
    await release_request_db(db)
    return _result

@router.get("/console/metrics/comparison", response_model=MetricsComparison)
@requires_permission('manage_settings')
async def get_console_metrics_comparison(
    params: MetricsQueryParams = Depends(),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """Get console metrics with previous period comparison"""
    _result = await console_service.get_metrics_with_comparison(db, organization, params)
    await release_request_db(db)
    return _result

@router.get("/console/recent-widgets")
@requires_permission('manage_settings')
async def get_recent_widgets(
    offset: int = 0,
    limit: int = 10,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """Get recent widgets for the console with pagination"""
    _result = await console_service.get_recent_widgets(db, organization, current_user, offset, limit)
    await release_request_db(db)
    return _result

@router.get("/console/metrics/timeseries", response_model=TimeSeriesMetrics)
@requires_permission('manage_settings')
async def get_timeseries_metrics(
    params: MetricsQueryParams = Depends(),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """Get time-series metrics data for charts"""
    _result = await console_service.get_timeseries_metrics(db, organization, params)
    await release_request_db(db)
    return _result

@router.get("/console/metrics/table-usage", response_model=TableUsageMetrics)
@requires_permission("manage_settings")
async def get_table_usage(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Get table usage statistics"""
    _result = await console_service.get_table_usage_metrics(db, organization, params)
    await release_request_db(db)
    return _result

@router.get("/console/metrics/table-joins-heatmap", response_model=TableJoinsHeatmap)
@requires_permission("manage_settings") 
async def get_table_joins_heatmap(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Get table joins heatmap data"""
    _result = await console_service.get_table_joins_heatmap(db, organization, params)
    await release_request_db(db)
    return _result

@router.get("/console/metrics/top-users", response_model=TopUsersMetrics)
@requires_permission("manage_settings")
async def get_top_users(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Get top users by activity with trend analysis"""
    _result = await console_service.get_top_users_metrics(db, organization, params)
    await release_request_db(db)
    return _result

@router.get("/console/metrics/tool-usage", response_model=ToolUsageMetrics)
@requires_permission("manage_settings")
async def get_tool_usage(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Get tool usage counts for key tools."""
    _result = await console_service.get_tool_usage_metrics(db, organization, params)
    await release_request_db(db)
    return _result

@router.get("/console/metrics/llm-usage", response_model=LLMUsageMetrics)
@requires_permission("manage_settings")
async def get_llm_usage(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """Get aggregated LLM token/cost usage per model."""
    _result = await console_service.get_llm_usage_metrics(db, organization, params)
    await release_request_db(db)
    return _result

@router.get("/console/metrics/cost", response_model=CostMetrics)
@require_enterprise(feature="cost_dashboard")
@requires_permission("manage_settings")
async def get_cost_metrics(
    group_by: str = "model",
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """Get LLM cost/token spend broken down by a dimension (model, provider,
    user, data_source, group, scope) with a daily timeseries."""
    _result = await console_service.get_cost_metrics(db, organization, params, group_by=group_by)
    await release_request_db(db)
    return _result

@router.get("/console/metrics/recent-negative-feedback", response_model=RecentNegativeFeedbackMetrics)
@requires_permission("manage_settings")
async def get_recent_negative_feedback(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Get recent negative feedback with completion context"""
    _result = await console_service.get_recent_negative_feedback_metrics(db, organization, params)
    await release_request_db(db)
    return _result




@router.get("/console/trace/{report_id}/{completion_id}", response_model=TraceData)
@requires_permission("manage_settings")
async def get_trace_data(
    report_id: str,
    completion_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Get detailed trace data for debugging"""
    _result = await console_service.get_trace_data(db, organization, report_id, completion_id)
    await release_request_db(db)
    return _result

@router.get("/console/issues/compact", response_model=CompactIssuesResponse)
@requires_permission("manage_settings")
async def get_compact_issues(
    params: MetricsQueryParams = Depends(),
    page: int = 1,
    page_size: int = 50,
    filter: Optional[str] = None,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Compact completion-anchored issues list (tool errors or negative feedback)."""
    _result = await console_service.get_compact_issues(db, organization, params, page, page_size, filter)
    await release_request_db(db)
    return _result


@router.get("/console/agent_executions/summaries", response_model=AgentExecutionSummariesResponse)
@requires_permission("manage_settings")
async def get_agent_execution_summaries(
    params: MetricsQueryParams = Depends(),
    page: int = 1,
    page_size: int = 20,
    filter: Optional[str] = None,
    tool_name: Optional[str] = None,
    prompt_search: Optional[str] = None,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Agent execution summaries joined with completion, feedback, and tool stats."""
    _result = await console_service.get_agent_execution_summaries(
        db, organization, params, page, page_size, filter, tool_name, prompt_search
    )
    await release_request_db(db)
    return _result

@router.get("/console/diagnosis/metrics")
@requires_permission("manage_settings")
async def get_diagnosis_dashboard_metrics(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Get dashboard metrics for diagnosis page."""
    _result = await console_service.get_diagnosis_dashboard_metrics(db, organization, params)
    await release_request_db(db)
    return _result

@router.get("/console/diagnosis/timeseries", response_model=DiagnosisTimeSeriesMetrics)
@requires_permission("manage_settings")
async def get_diagnosis_timeseries(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Get agent executions bucketed daily by status for the diagnosis activity chart."""
    _result = await console_service.get_diagnosis_timeseries(db, organization, params)
    await release_request_db(db)
    return _result

