"""Overnight learning — the current user's side.

- the session-start briefing ("Since you were here") and its feedback,
- habit offers (accept creates a normal scheduled prompt),
- the per-user "Prepare things for me overnight" toggle,
- "What I did overnight": the user's own dream runs (owner-only).

Every route is scoped to (current user, current organization); there is no
way to address another user's items.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.dependencies import get_async_db, get_current_organization
from app.errors import AppError, ErrorCode
from app.models.membership import Membership
from app.models.organization import Organization
from app.models.user import User
from app.services.dreams import briefing as B
from app.services.dreams import common as C

router = APIRouter(tags=["users"])


class BriefingResponse(BaseModel):
    items: List[Dict[str, Any]] = []
    since: Optional[str] = None


class BriefingFeedback(BaseModel):
    useful: bool = False


class OvernightPrefs(BaseModel):
    # "Prepare things for me overnight".
    enabled: bool = True
    # Read-only: whether the org turned overnight preparation on at all.
    available: bool = False


class OvernightLogResponse(BaseModel):
    runs: List[Dict[str, Any]] = []


async def _membership(db: AsyncSession, user: User, organization: Organization) -> Optional[Membership]:
    return (
        await db.execute(
            select(Membership).where(
                Membership.organization_id == str(organization.id), Membership.user_id == str(user.id),
            )
        )
    ).scalar_one_or_none()


async def _available(db: AsyncSession, organization: Organization) -> bool:
    from app.services.dreams.runtime import load_org_settings

    return C.user_dreaming_enabled(await load_org_settings(db, str(organization.id)))


@router.get("/users/me/briefing", response_model=BriefingResponse)
async def get_my_briefing(
    report_id: Optional[str] = Query(None),
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    data = await B.get_briefing(db, organization_id=str(organization.id), user_id=str(user.id), report_id=report_id)
    return BriefingResponse(**data)


@router.post("/users/me/briefing/seen", response_model=BriefingResponse)
async def mark_my_briefing_seen(
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    await B.mark_seen(db, organization_id=str(organization.id), user_id=str(user.id))
    return BriefingResponse(items=[])


@router.post("/users/me/briefing/items/{kind}/{item_id}/feedback")
async def briefing_item_feedback(
    kind: str,
    item_id: str,
    payload: BriefingFeedback,
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    ok = await B.record_feedback(
        db, organization_id=str(organization.id), user_id=str(user.id), kind=kind, item_id=item_id,
        useful=payload.useful,
    )
    if not ok:
        raise AppError.not_found(ErrorCode.BRIEFING_ITEM_NOT_FOUND, "Briefing item not found.")
    return {"ok": True}


def _offer_out(o) -> Dict[str, Any]:
    return {
        "id": str(o.id), "status": o.status, "report_id": str(o.report_id), "intent": o.intent_text,
        "cadence": o.cadence, "time": o.suggested_time,
        "scheduled_prompt_id": str(o.scheduled_prompt_id) if o.scheduled_prompt_id else None,
    }


@router.post("/users/me/habit_offers/{offer_id}/accept")
async def accept_habit_offer(
    offer_id: str,
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    offer, err = await B.accept_habit(db, organization=organization, user=user, offer_id=offer_id)
    if err == "not_found":
        raise AppError.not_found(ErrorCode.HABIT_OFFER_NOT_FOUND, "Offer not found.")
    if err == "not_pending":
        raise AppError.bad_request(ErrorCode.HABIT_OFFER_NOT_PENDING, "This offer was already answered.")
    return _offer_out(offer)


@router.post("/users/me/habit_offers/{offer_id}/decline")
async def decline_habit_offer(
    offer_id: str,
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    offer, err = await B.decline_habit(
        db, organization_id=str(organization.id), user_id=str(user.id), offer_id=offer_id,
    )
    if err == "not_found":
        raise AppError.not_found(ErrorCode.HABIT_OFFER_NOT_FOUND, "Offer not found.")
    if err == "not_pending":
        raise AppError.bad_request(ErrorCode.HABIT_OFFER_NOT_PENDING, "This offer was already answered.")
    return _offer_out(offer)


@router.get("/users/me/overnight", response_model=OvernightPrefs)
async def get_my_overnight(
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    m = await _membership(db, user, organization)
    return OvernightPrefs(enabled=bool(getattr(m, "overnight_prep", True)), available=await _available(db, organization))


@router.put("/users/me/overnight", response_model=OvernightPrefs)
async def update_my_overnight(
    payload: OvernightPrefs,
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    m = await _membership(db, user, organization)
    if m is None:
        raise AppError.not_found(ErrorCode.MEMBERSHIP_NOT_FOUND, "Membership not found.")
    m.overnight_prep = bool(payload.enabled)
    db.add(m)
    await db.commit()
    return OvernightPrefs(enabled=m.overnight_prep, available=await _available(db, organization))


@router.get("/users/me/overnight/log", response_model=OvernightLogResponse)
async def get_my_overnight_log(
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    runs = await B.overnight_log(db, organization_id=str(organization.id), user_id=str(user.id))
    return OvernightLogResponse(runs=runs)
