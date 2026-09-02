# Enterprise Routes
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from app.ee.license import (
    get_key_source,
    get_license_info,
    get_max_users,
    get_max_agents,
    has_config_license_key,
    mask_license_key,
    LicenseInfo,
)
from app.ee.audit.routes import router as audit_router
from app.ee.scim.routes import scim_admin_router
from app.ee.ldap.routes import ldap_admin_router
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.models.user import User
from app.models.organization import Organization
from app.models.membership import Membership
from app.models.data_source import DataSource

logger = logging.getLogger(__name__)

router = APIRouter(tags=["enterprise"])

# Include sub-routers
router.include_router(audit_router)
router.include_router(scim_admin_router)
router.include_router(ldap_admin_router)


@router.get("/license", response_model=LicenseInfo)
async def get_license_status():
    """
    Get current license status.
    This endpoint is public and does not require authentication.
    """
    return get_license_info()


class LicenseUsage(BaseModel):
    """Current per-organization usage against the license quotas.

    A max of -1 means unlimited. Counts reflect this organization only.
    """
    max_users: int = -1
    current_users: int = 0
    max_agents: int = -1
    current_agents: int = 0


@router.get("/license/usage", response_model=LicenseUsage)
async def get_license_usage(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """
    Current usage for the active organization against its license quotas.

    Unlike /license, this is organization-scoped (requires auth + org context) so
    the UI can render a "used / allowed" seat and agent readout. Members include
    pending invites; agents are data sources.
    """
    users_result = await db.execute(
        select(func.count(Membership.id)).where(
            Membership.organization_id == organization.id
        )
    )
    agents_result = await db.execute(
        select(func.count(DataSource.id)).where(
            DataSource.organization_id == organization.id
        )
    )
    return LicenseUsage(
        max_users=get_max_users(),
        current_users=users_result.scalar() or 0,
        max_agents=get_max_agents(),
        current_agents=agents_result.scalar() or 0,
    )


# --- License key management (Settings → License) ---------------------------
#
# GET /license above stays public and key-free. Everything below is admin-only
# and never returns the key itself, only where it came from and a masked tail.
#
# Note on scope: `manage_settings` is an *organization* permission while the
# license is instance-wide. On a single-organization deployment (the common
# case) these coincide. On a multi-organization instance any org's admin can
# re-license the whole instance — accepted deliberately, with the audit entries
# below as the accountability trail.


class LicenseKeyStatus(BaseModel):
    """Where the active license key comes from, without disclosing it."""

    # "database" (set in the UI), "config" (bow-config / BOW_LICENSE_KEY), or "none".
    source: str = "none"
    masked_key: Optional[str] = None
    # True when the deployment also supplies a key; combined with
    # source == "database" this means the stored key is overriding it.
    config_key_present: bool = False
    updated_at: Optional[str] = None
    license: LicenseInfo


class LicenseKeyUpdate(BaseModel):
    key: str


async def _license_key_status(db: AsyncSession) -> LicenseKeyStatus:
    from app.services.license_service import get_stored_license
    from app.ee.license import get_db_license_key

    stored = await get_stored_license(db)
    source = get_key_source()
    active_key = get_db_license_key() if source == "database" else None
    return LicenseKeyStatus(
        source=source,
        masked_key=mask_license_key(active_key),
        config_key_present=has_config_license_key(),
        updated_at=stored.get("updated_at") if stored.get("key") else None,
        license=get_license_info(),
    )


async def _audit_license(db, organization, user, action: str, details: dict) -> None:
    """Record who changed the instance license. Never logs the key itself."""
    try:
        from app.ee.audit.service import audit_service
        await audit_service.log(
            db=db,
            organization_id=str(organization.id) if organization else None,
            action=action,
            user_id=str(user.id) if user else None,
            resource_type="instance_settings",
            resource_id="license",
            details=details,
        )
    except Exception:
        logger.debug("license audit log failed", exc_info=True)


@router.get("/license/key", response_model=LicenseKeyStatus)
@requires_permission('manage_settings')
async def get_license_key_status(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Admin view of the license key: its source and masked tail, never the key."""
    return await _license_key_status(db)


@router.put("/license/key", response_model=LicenseKeyStatus)
@requires_permission('manage_settings')
async def update_license_key(
    data: LicenseKeyUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Activate a license key for this instance, overriding BOW_LICENSE_KEY.

    The key is verified before it is stored, so an invalid or expired key is
    rejected outright rather than replacing a working license.
    """
    from app.services.license_service import save_license_key

    try:
        info = await save_license_key(db, data.key, str(current_user.id))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await _audit_license(
        db, organization, current_user, "settings.license_key_updated",
        {"license_id": info.license_id, "tier": info.tier, "org_name": info.org_name},
    )
    return await _license_key_status(db)


@router.delete("/license/key", response_model=LicenseKeyStatus)
@requires_permission('manage_settings')
async def delete_license_key(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Remove the stored key and fall back to bow-config / BOW_LICENSE_KEY."""
    from app.services.license_service import remove_license_key

    info = await remove_license_key(db, str(current_user.id))
    await _audit_license(
        db, organization, current_user, "settings.license_key_removed",
        {"fell_back_to": get_key_source(), "tier": info.tier},
    )
    return await _license_key_status(db)
