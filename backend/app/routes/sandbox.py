"""Settings → Sandbox: the admin API for agent sandboxes.

Every route here is behind two gates, in this order:

1. the deployment flag (``require_sandbox_feature``): with it off every route
   answers ``agent_sandboxes.disabled``;
2. the ``manage_sandboxes`` permission, which is separate from
   ``manage_settings``.

Responses never include private keys, registry passwords, secret values or
sandbox tokens.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.models.organization import Organization
from app.models.user import User
from app.services.sandbox.feature import require_sandbox_feature, sandboxes_enabled_for_org

router = APIRouter(
    prefix="/sandbox",
    tags=["sandbox"],
    dependencies=[Depends(require_sandbox_feature)],
)


@router.get("/status")
@requires_permission("manage_sandboxes")
async def get_sandbox_status(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Where the feature stands for this organization (the three gate levels)."""
    from app.services.organization_settings_service import OrganizationSettingsService

    org_settings = await OrganizationSettingsService().get_settings(db, organization, current_user)
    return {
        "feature_enabled": True,
        "enabled": sandboxes_enabled_for_org(org_settings),
    }
