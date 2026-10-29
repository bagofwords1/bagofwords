# Audit Log Stream routes
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

from typing import List

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.ee.audit.streams.schemas import (
    DestinationSpec,
    StreamCreate,
    StreamResponse,
    StreamTestRequest,
    StreamTestResult,
    StreamUpdate,
)
from app.ee.audit.streams.service import audit_stream_service, destination_specs
from app.ee.license import require_enterprise
from app.models.organization import Organization
from app.models.user import User

# Mounted before the audit router: its "/{log_id}" would otherwise capture
# "/streams".
router = APIRouter(prefix="/enterprise/audit/streams", tags=["enterprise", "audit"])

FEATURE = "audit_log_streams"


@router.get("/destinations", response_model=List[DestinationSpec])
@require_enterprise(feature=FEATURE)
@requires_permission("view_audit_logs")
async def list_destinations(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Form schema for every destination type (fields, which are secret, defaults)."""
    return destination_specs()


@router.get("", response_model=List[StreamResponse])
@require_enterprise(feature=FEATURE)
@requires_permission("view_audit_logs")
async def list_streams(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await audit_stream_service.list(db, str(organization.id))


@router.post("", response_model=StreamResponse)
@require_enterprise(feature=FEATURE)
@requires_permission("manage_settings")
async def create_stream(
    body: StreamCreate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await audit_stream_service.create(db, str(organization.id), str(current_user.id), body, request=request)


@router.post("/test", response_model=StreamTestResult)
@require_enterprise(feature=FEATURE)
@requires_permission("manage_settings")
async def test_stream(
    body: StreamTestRequest,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Send one synthetic ``audit_stream.test`` event with the given settings."""
    return await audit_stream_service.test(db, str(organization.id), current_user, body)


@router.get("/{stream_id}", response_model=StreamResponse)
@require_enterprise(feature=FEATURE)
@requires_permission("view_audit_logs")
async def get_stream(
    stream_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await audit_stream_service.get(db, str(organization.id), stream_id)


@router.patch("/{stream_id}", response_model=StreamResponse)
@require_enterprise(feature=FEATURE)
@requires_permission("manage_settings")
async def update_stream(
    stream_id: str,
    body: StreamUpdate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await audit_stream_service.update(db, str(organization.id), str(current_user.id), stream_id, body, request=request)


@router.delete("/{stream_id}", status_code=204)
@require_enterprise(feature=FEATURE)
@requires_permission("manage_settings")
async def delete_stream(
    stream_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    await audit_stream_service.delete(db, str(organization.id), str(current_user.id), stream_id, request=request)
