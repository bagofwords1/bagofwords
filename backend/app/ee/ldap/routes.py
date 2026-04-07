# LDAP Admin Routes
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.ee.license import require_enterprise
from app.ee.ldap.connection import LDAPConnectionManager
from app.ee.ldap.sync_service import LDAPGroupSyncService
from app.ee.ldap.schemas import (
    SyncResult,
    SyncStatus,
    LDAPSyncPreview,
    LDAPTestResult,
)
from app.settings.config import settings
from app.models.user import User
from app.models.organization import Organization

ldap_admin_router = APIRouter(prefix="/enterprise/ldap", tags=["enterprise", "ldap"])

# In-memory sync status per org (simple; could be DB-backed later)
_sync_status: dict[str, SyncResult] = {}


def _get_ldap_config():
    config = settings.bow_config.ldap
    if not config.enabled:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="LDAP is not configured")
    return config


@ldap_admin_router.post("/sync", response_model=SyncResult)
@require_enterprise(feature="ldap")
@requires_permission("manage_identity_providers")
async def trigger_sync(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Trigger an immediate LDAP group sync for this organization."""
    config = _get_ldap_config()
    sync_service = LDAPGroupSyncService(config)
    result = await sync_service.sync_groups(db, str(organization.id))
    _sync_status[str(organization.id)] = result
    return result


@ldap_admin_router.get("/sync/status", response_model=SyncStatus)
@require_enterprise(feature="ldap")
@requires_permission("manage_identity_providers")
async def get_sync_status(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Get the last sync result for this organization."""
    org_id = str(organization.id)
    return SyncStatus(
        last_sync=_sync_status.get(org_id),
        is_syncing=False,
        ldap_configured=settings.bow_config.ldap.enabled,
    )


@ldap_admin_router.get("/sync/preview", response_model=LDAPSyncPreview)
@require_enterprise(feature="ldap")
@requires_permission("manage_identity_providers")
async def preview_sync(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Dry-run: show what a sync would change without writing."""
    config = _get_ldap_config()
    sync_service = LDAPGroupSyncService(config)
    return await sync_service.preview_sync(db, str(organization.id))


@ldap_admin_router.get("/test-connection", response_model=LDAPTestResult)
@require_enterprise(feature="ldap")
@requires_permission("manage_identity_providers")
async def test_connection(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Test LDAP server connectivity."""
    config = _get_ldap_config()
    manager = LDAPConnectionManager(config)
    conn_result = manager.test_connection()

    test_result = LDAPTestResult(
        connected=conn_result["connected"],
        server=conn_result["server"],
        vendor=conn_result.get("vendor"),
        error=conn_result.get("error"),
    )

    # If connected, try to count users and groups
    if test_result.connected:
        try:
            users = manager.search_users()
            test_result.user_count = len(users)
        except Exception:
            pass
        try:
            groups = manager.search_groups()
            test_result.group_count = len(groups)
        except Exception:
            pass

    return test_result
