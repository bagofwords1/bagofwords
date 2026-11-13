"""Instance admin's product-updates (release notes) opt-in."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.models.organization import Organization
from app.models.user import User
from app.services import product_updates_service as svc
from app.settings.config import settings

router = APIRouter(tags=["product-updates"])


class ProductUpdatesStatus(BaseModel):
    # False when telemetry is off or this is the cloud deployment: the UI
    # hides the prompt and the toggle entirely.
    available: bool
    # Whether the admin has answered yet (onboarding asks only when False).
    decided: bool
    opted_in: bool
    # The address receiving updates while subscribed.
    email: Optional[str] = None
    # The address that would be subscribed if the caller opts in now.
    current_user_email: Optional[str] = None


class ProductUpdatesUpdate(BaseModel):
    opted_in: bool
    source: Literal["onboarding", "settings"] = "settings"


def _ensure_instance_admin(user: User) -> None:
    """Same rule as the license key: the choice is instance-wide, so on a
    multi-organization instance only an instance superuser may make it."""
    if settings.bow_config.features.allow_multiple_organizations and not user.is_superuser:
        raise HTTPException(
            status_code=403,
            detail="Only an instance administrator can change product updates on a multi-organization instance",
        )


async def _status(db: AsyncSession, user: User) -> ProductUpdatesStatus:
    decision = await svc.get_decision(db)
    return ProductUpdatesStatus(
        available=svc.prompt_available(),
        decided=bool(decision),
        opted_in=bool(decision.get("opted_in")),
        email=decision.get("email"),
        current_user_email=user.email,
    )


@router.get("/product-updates", response_model=ProductUpdatesStatus)
@requires_permission("manage_settings")
async def get_product_updates(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await _status(db, current_user)


@router.put("/product-updates", response_model=ProductUpdatesStatus)
@requires_permission("manage_settings")
async def update_product_updates(
    data: ProductUpdatesUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    _ensure_instance_admin(current_user)
    if not svc.prompt_available():
        raise HTTPException(status_code=409, detail="Product updates are not available on this instance")
    await svc.record_decision(
        db,
        user_id=str(current_user.id),
        email=current_user.email,
        opted_in=data.opted_in,
        source=data.source,
    )
    return await _status(db, current_user)
