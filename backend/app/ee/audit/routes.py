# Audit Log Routes
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details

from typing import Optional
from datetime import datetime
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.ee.license import require_enterprise
from app.ee.audit.service import audit_service
from app.ee.audit.schemas import (
    AuditLogResponse,
    AuditLogListResponse,
    AuditLogFilters,
)
from app.models.user import User
from app.models.organization import Organization

router = APIRouter(prefix="/enterprise/audit", tags=["enterprise", "audit"])


@router.get("", response_model=AuditLogListResponse)
@require_enterprise(feature="audit_logs")
@requires_permission("view_audit_logs")
async def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    action: Optional[str] = Query(None),
    resource_type: Optional[str] = Query(None),
    resource_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    search: Optional[str] = Query(None),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """
    List audit logs for the organization.
    Enterprise feature - requires audit_logs license feature.
    """
    filters = AuditLogFilters(
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        user_id=user_id,
        start_date=start_date,
        end_date=end_date,
        search=search,
    )

    logs, total = await audit_service.get_logs(
        db=db,
        organization_id=str(organization.id),
        filters=filters,
        page=page,
        page_size=page_size,
    )

    total_pages = (total + page_size - 1) // page_size

    return AuditLogListResponse(
        items=logs,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/action-types", response_model=list[str])
@require_enterprise(feature="audit_logs")
@requires_permission("view_audit_logs")
async def get_action_types(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """
    Get list of available action types for filtering.
    Enterprise feature - requires audit_logs license feature.
    """
    action_types = await audit_service.get_action_types(
        db=db,
        organization_id=str(organization.id),
    )
    
    return action_types


@router.get("/{log_id}", response_model=AuditLogResponse)
@require_enterprise(feature="audit_logs")
@requires_permission("view_audit_logs")
async def get_audit_log(
    log_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """
    Get a single audit log entry by ID.
    Enterprise feature - requires audit_logs license feature.
    """
    log = await audit_service.get_log_by_id(
        db=db,
        organization_id=str(organization.id),
        log_id=log_id,
    )

    if not log:
        raise HTTPException(status_code=404, detail="Audit log not found")

    return log
