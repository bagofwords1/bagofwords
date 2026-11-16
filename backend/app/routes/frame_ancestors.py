"""Sites allowed to show BOW in an iframe (Settings → General).

Instance-wide like the license, so on a multi-organization instance only an
instance superuser may change it; with a single organization its admins own
the instance.
"""

import logging
from typing import Annotated, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.models.organization import Organization
from app.models.user import User
from app.services import frame_ancestors_service as svc
from app.settings.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["frame-ancestors"])


class FrameAncestorsStatus(BaseModel):
    origins: List[str]
    can_edit: bool


class FrameAncestorsUpdate(BaseModel):
    origins: List[Annotated[str, Field(max_length=2048)]] = Field(default_factory=list, max_length=svc.MAX_ORIGINS * 2)


def _can_edit(user: User) -> bool:
    return not settings.bow_config.features.allow_multiple_organizations or bool(user.is_superuser)


async def _status(db: AsyncSession, user: User) -> FrameAncestorsStatus:
    return FrameAncestorsStatus(
        origins=await svc.get_origins(db), can_edit=_can_edit(user)
    )


async def _audit(db, organization, user, before: List[str], after: List[str]) -> None:
    try:
        from app.ee.audit.service import audit_service

        await audit_service.log(
            db=db,
            organization_id=str(organization.id) if organization else None,
            action="settings.frame_ancestors_updated",
            user_id=str(user.id),
            resource_type="instance_settings",
            resource_id=svc.SETTING_KEY,
            details={"added": [o for o in after if o not in before], "removed": [o for o in before if o not in after]},
        )
    except Exception:
        logger.debug("frame ancestors audit log failed", exc_info=True)


@router.get("/settings/frame-ancestors", response_model=FrameAncestorsStatus)
@requires_permission("manage_settings")
async def get_frame_ancestors(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await _status(db, current_user)


@router.put("/settings/frame-ancestors", response_model=FrameAncestorsStatus)
@requires_permission("manage_settings")
async def update_frame_ancestors(
    data: FrameAncestorsUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    if not _can_edit(current_user):
        raise HTTPException(
            status_code=403,
            detail="Only an instance administrator can change the allowed sites on a multi-organization instance",
        )
    before = await svc.get_origins(db)
    after = await svc.save_origins(db, data.origins, user_id=str(current_user.id))
    if after != before:
        await _audit(db, organization, current_user, before, after)
    return await _status(db, current_user)
