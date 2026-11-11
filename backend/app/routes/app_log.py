"""Settings → System logs: read, clear and switch on/off the app_logs capture.

These are the deployment's own logs, not an organization's data, so access
follows the instance license (``app/ee/routes.py``): an organization admin
(``full_admin_access``), and on an instance that allows more than one
organization, where anyone can create an org and become its admin, only an
instance superuser.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.errors import AppError, ErrorCode
from app.models.organization import Organization
from app.models.user import User
from app.schemas.app_log_schema import (
    AppLogDeleteResult,
    AppLogEntry,
    AppLogList,
    AppLogSettings,
    AppLogSettingsUpdate,
)
from app.services import app_log_service as svc

router = APIRouter(tags=["system-logs"])
logger = logging.getLogger(__name__)


def _ensure_instance_admin(user: User) -> None:
    from app.settings.config import settings

    if settings.bow_config.features.allow_multiple_organizations and not user.is_superuser:
        raise AppError.forbidden(
            ErrorCode.SYSTEM_LOGS_INSTANCE_ADMIN_REQUIRED,
            "Only an instance administrator can use system logs on a multi-organization instance",
        )


def _naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


async def _audit(db, organization, user, action: str, details: dict) -> None:
    try:
        from app.ee.audit.service import audit_service

        await audit_service.log(
            db=db,
            organization_id=str(organization.id) if organization else None,
            action=action,
            user_id=str(user.id) if user else None,
            resource_type="instance_settings",
            resource_id=svc.SETTING_KEY,
            details=details,
        )
    except Exception:
        logger.debug("system logs audit failed", exc_info=True)


async def _settings_response(db: AsyncSession) -> AppLogSettings:
    await svc.refresh_state(db)
    status = svc.capture_status()
    return AppLogSettings(
        enabled=status["enabled"],
        expires_at=status["expires_at"],
        instance_id=svc.INSTANCE_ID,
        instances=await svc.list_instances(db),
        retention_days=svc.RETENTION.days,
        max_size_bytes=svc.MAX_TABLE_BYTES,
        size_bytes=await svc.table_size_bytes(db),
    )


@router.get("/system-logs", response_model=AppLogList)
@requires_permission('full_admin_access')
async def list_system_logs(
    start: Optional[datetime] = Query(None, description="Only lines at or after this time"),
    end: Optional[datetime] = Query(None, description="Only lines at or before this time"),
    instance_id: Optional[str] = Query(None, max_length=255),
    errors_only: bool = Query(False, description="Only ERROR/CRITICAL records and stderr output"),
    search: Optional[str] = Query(None, max_length=500, description="Case-insensitive substring of the message"),
    limit: int = Query(svc.DEFAULT_LIMIT, ge=1, le=svc.MAX_LIMIT),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    _ensure_instance_admin(current_user)
    start, end = _naive_utc(start), _naive_utc(end)
    if start and end and start > end:
        raise AppError.bad_request(ErrorCode.SYSTEM_LOGS_INVALID_RANGE, "start must be before end")
    # Write what this instance has buffered first, so the newest lines show.
    await svc.flush()
    rows, has_more = await svc.query_logs(
        db,
        start=start,
        end=end,
        instance_id=instance_id or None,
        errors_only=errors_only,
        search=(search or "").strip() or None,
        limit=limit,
    )
    return AppLogList(
        items=[AppLogEntry.model_validate(r) for r in rows],
        limit=limit,
        has_more=has_more,
    )


@router.get("/system-logs/settings", response_model=AppLogSettings)
@requires_permission('full_admin_access')
async def get_system_logs_settings(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    _ensure_instance_admin(current_user)
    return await _settings_response(db)


@router.put("/system-logs/settings", response_model=AppLogSettings)
@requires_permission('full_admin_access')
async def update_system_logs_settings(
    payload: AppLogSettingsUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    _ensure_instance_admin(current_user)
    await svc.set_enabled(
        db,
        enabled=payload.enabled,
        duration_minutes=payload.duration_minutes,
        user_id=str(current_user.id),
    )
    await _audit(
        db, organization, current_user,
        "system_logs.enabled" if payload.enabled else "system_logs.disabled",
        {"duration_minutes": payload.duration_minutes} if payload.enabled else {},
    )
    return await _settings_response(db)


@router.delete("/system-logs", response_model=AppLogDeleteResult)
@requires_permission('full_admin_access')
async def delete_system_logs(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    _ensure_instance_admin(current_user)
    deleted = await svc.delete_all(db)
    await _audit(db, organization, current_user, "system_logs.cleared", {"deleted": deleted})
    return AppLogDeleteResult(deleted=deleted)
