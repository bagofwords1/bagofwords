"""Org setting `allow_public_sharing`: may reports be shared with anyone who
has the link?

When an org turns it off:
- no new public share can be created (see assert_public_sharing_allowed);
- a report already stored as 'public' is treated as 'internal' by the access
  check (ReportService._check_visibility): signed-in members of the report's
  org still open it, anonymous visitors and other orgs are refused.

The stored visibility is never rewritten, so turning the setting back on
restores the same links.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, ErrorCode

SETTING_KEY = "allow_public_sharing"


async def public_sharing_allowed(db: AsyncSession, organization_id) -> bool:
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
        # Entry stored without its metadata (e.g. {"value": false}); see
        # OrganizationSettingsService._repair_partial_feature.
        value = cfg.get("value", True)
    else:
        value = cfg
    return bool(value)


async def assert_public_sharing_allowed(db: AsyncSession, organization_id) -> None:
    if not await public_sharing_allowed(db, organization_id):
        raise AppError.forbidden(
            ErrorCode.SHARING_PUBLIC_DISABLED,
            "Public link sharing is turned off for this organization",
        )
