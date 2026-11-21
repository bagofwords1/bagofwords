"""Settings → System logs: read and clear the server's own log files.

Only instance superusers may access these deployment-wide logs, and only
when the deployment turns the feature on (``system_logs.enabled`` /
``BOW_SYSTEM_LOGS_ENABLED``).

The files are on disk (``app_log_service``), so every read runs in a thread
to keep the event loop free.
"""
import asyncio
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
    AppLogStatus,
)
from app.services import app_log_service as svc
from app.settings.config import settings

logger = logging.getLogger(__name__)


async def require_log_admin(user: User = Depends(current_user)) -> None:
    if not user.is_superuser or user.is_service_account:
        raise AppError.forbidden(
            ErrorCode.SYSTEM_LOGS_INSTANCE_ADMIN_REQUIRED,
            "Only an instance administrator can use system logs",
        )


async def require_enabled() -> None:
    # After the admin check, so the answer to anyone else stays a plain 403.
    if not settings.bow_config.system_logs.enabled:
        raise AppError.not_found(
            ErrorCode.SYSTEM_LOGS_DISABLED,
            "System logs are turned off on this server (BOW_SYSTEM_LOGS_ENABLED)",
        )


router = APIRouter(
    tags=["system-logs"],
    dependencies=[Depends(require_log_admin), Depends(require_enabled)],
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
            resource_type="system_logs",
            resource_id=None,
            details=details,
        )
    except Exception:
        logger.debug("system logs audit failed", exc_info=True)


def _status() -> AppLogStatus:
    config = svc.get_config()
    return AppLogStatus(
        instance_id=svc.INSTANCE_ID,
        instances=svc.list_instances(config),
        directory=str(config.directory),
        retention_days=config.retention.days,
        max_size_bytes=config.max_bytes,
        size_bytes=svc.size_bytes(config),
        **svc.writer_status(),
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
    # Not used here; requires_permission checks membership with it.
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    start, end = _naive_utc(start), _naive_utc(end)
    if start and end and start > end:
        raise AppError.bad_request(ErrorCode.SYSTEM_LOGS_INVALID_RANGE, "start must be before end")

    def read():
        # Write what this process has buffered first, so the newest lines show.
        svc.flush()
        return svc.query_logs(
            start=start,
            end=end,
            instance_id=instance_id or None,
            errors_only=errors_only,
            search=(search or "").strip() or None,
            limit=limit,
        )

    rows, has_more = await asyncio.to_thread(read)
    return AppLogList(
        items=[AppLogEntry(**r) for r in rows],
        limit=limit,
        has_more=has_more,
    )


@router.get("/system-logs/status", response_model=AppLogStatus)
@requires_permission('full_admin_access')
async def get_system_logs_status(
    current_user: User = Depends(current_user),
    # Not used here; requires_permission checks membership with it.
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await asyncio.to_thread(_status)


@router.delete("/system-logs", response_model=AppLogDeleteResult)
@requires_permission('full_admin_access')
async def delete_system_logs(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    try:
        deleted = await asyncio.to_thread(svc.delete_all)
    except OSError as e:
        directory = str(svc.get_config().directory)
        logger.warning(f"System logs: could not delete the log files in {directory}: {e}")
        raise AppError(
            ErrorCode.SYSTEM_LOGS_DELETE_FAILED,
            f"Could not delete the log files in {directory}",
            status_code=503,
            params={"directory": directory},
        )
    await _audit(db, organization, current_user, "system_logs.cleared", {"deleted_files": deleted})
    return AppLogDeleteResult(deleted_files=deleted)
