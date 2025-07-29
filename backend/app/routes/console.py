from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db, get_current_organization
from app.services.console_service import ConsoleService
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.schemas.console_schema import SimpleMetrics, MetricsQueryParams, MetricsComparison, TimeSeriesMetrics, TableUsageData, TableUsageMetrics, TableJoinsHeatmap, TableJoinData
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
from app.schemas.console_schema import TopUsersMetrics, RecentNegativeFeedbackMetrics, DiagnosisMetrics, TraceData

logger = logging.getLogger(__name__)

router = APIRouter(tags=["console"])
console_service = ConsoleService()

@router.get("/console/metrics", response_model=SimpleMetrics)
@requires_permission('view_organization_overview')
async def get_console_metrics(
    params: MetricsQueryParams = Depends(),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """Get console metrics with optional date filtering"""
    return await console_service.get_organization_metrics(db, organization, params)

@router.get("/console/metrics/comparison", response_model=MetricsComparison)
@requires_permission('view_organization_overview')
async def get_console_metrics_comparison(
    params: MetricsQueryParams = Depends(),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """Get console metrics with previous period comparison"""
    return await console_service.get_metrics_with_comparison(db, organization, params)

@router.get("/console/recent-widgets")
@requires_permission('view_organization_overview')
async def get_recent_widgets(
    offset: int = 0,
    limit: int = 10,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """Get recent widgets for the console with pagination"""
    return await console_service.get_recent_widgets(db, organization, current_user, offset, limit)

@router.get("/console/metrics/timeseries", response_model=TimeSeriesMetrics)
@requires_permission('view_organization_overview')
async def get_timeseries_metrics(
    params: MetricsQueryParams = Depends(),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """Get time-series metrics data for charts"""
    return await console_service.get_timeseries_metrics(db, organization, params)

@router.get("/console/metrics/table-usage", response_model=TableUsageMetrics)
@requires_permission("view_organization_overview")
async def get_table_usage(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Get table usage statistics"""
    return await console_service.get_table_usage_metrics(db, organization, params)

@router.get("/console/metrics/table-joins-heatmap", response_model=TableJoinsHeatmap)
@requires_permission("view_organization_overview") 
async def get_table_joins_heatmap(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Get table joins heatmap data"""
    return await console_service.get_table_joins_heatmap(db, organization, params)

@router.get("/console/metrics/top-users", response_model=TopUsersMetrics)
@requires_permission("view_organization_overview")
async def get_top_users(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Get top users by activity with trend analysis"""
    return await console_service.get_top_users_metrics(db, organization, params)

@router.get("/console/metrics/recent-negative-feedback", response_model=RecentNegativeFeedbackMetrics)
@requires_permission("view_organization_overview")
async def get_recent_negative_feedback(
    params: MetricsQueryParams = Depends(),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Get recent negative feedback with completion context"""
    return await console_service.get_recent_negative_feedback_metrics(db, organization, params)


@router.get("/console/metrics/diagnosis", response_model=DiagnosisMetrics)
@requires_permission("view_organization_overview")
async def get_diagnosis_metrics(
    params: MetricsQueryParams = Depends(),
    page: int = 1,
    page_size: int = 50,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Get diagnosis metrics for failed steps and negative feedback"""
    return await console_service.get_diagnosis_metrics(db, organization, params, page, page_size)

@router.get("/console/trace/{report_id}/{completion_id}", response_model=TraceData)
@requires_permission("view_organization_overview")
async def get_trace_data(
    report_id: str,
    completion_id: str,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    """Get detailed trace data for debugging"""
    return await console_service.get_trace_data(db, organization, report_id, completion_id)