# Enterprise Routes
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from app.ee.license import get_license_info, get_max_users, get_max_agents, LicenseInfo
from app.ee.audit.routes import router as audit_router
from app.ee.scim.routes import scim_admin_router
from app.ee.ldap.routes import ldap_admin_router
from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.models.user import User
from app.models.organization import Organization
from app.models.membership import Membership
from app.models.data_source import DataSource

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
