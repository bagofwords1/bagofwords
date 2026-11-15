"""Org setting `allow_access_requests`: may a member ask a dashboard's editors
for view access from the no-access page?

On by default. When an org turns it off:
- the no-access page shows no "Request access" button (just names the owner),
- the backend refuses new requests (see assert_access_requests_allowed),
- requests already sent stay visible to editors, who can still answer them.

Mirrors public_sharing_policy.py.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, ErrorCode

SETTING_KEY = "allow_access_requests"


async def access_requests_allowed(db: AsyncSession, organization_id) -> bool:
    from app.models.organization_settings import OrganizationSettings

    settings = (await db.execute(
        select(OrganizationSettings).where(
            OrganizationSettings.organization_id == str(organization_id)
        )
    )).scalar_one_or_none()
    if settings is None:
        return True
    cfg = settings.get_config(SETTING_KEY)
    if cfg is None:
        return True
    if hasattr(cfg, "value"):
        value = cfg.value
    elif isinstance(cfg, dict):
        value = cfg.get("value", True)
    else:
        value = cfg
    return bool(value)


async def assert_access_requests_allowed(db: AsyncSession, organization_id) -> None:
    if not await access_requests_allowed(db, organization_id):
        raise AppError.forbidden(
            ErrorCode.ACCESS_REQUEST_DISABLED,
            "Access requests are turned off for this organization",
        )
