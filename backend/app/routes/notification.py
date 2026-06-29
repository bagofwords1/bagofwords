"""Notification inbox API — per-user.

Every handler is scoped to ``current_user``: a notification belongs to its
recipient, who owns its read/dismiss state. No org/agent permission resolution
is needed on read — delivery already decided the audience.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.dependencies import get_async_db, get_current_organization
from app.models.organization import Organization
from app.models.user import User
from app.services.inbox_service import inbox_service

router = APIRouter(tags=["notifications"])


class ReadBody(BaseModel):
    read: bool = True


class ReadAllBody(BaseModel):
    source: Optional[str] = None


@router.get("/notifications")
async def list_notifications(
    source: Optional[str] = Query(None),
    unread: Optional[bool] = Query(None),
    include_dismissed: bool = Query(False),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await inbox_service.list_for_user(
        db, current_user, source=source, unread=unread,
        include_dismissed=include_dismissed,
    )


@router.get("/notifications/count")
async def count_notifications(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await inbox_service.count_unread(db, current_user)


@router.post("/notifications/read-all")
async def read_all_notifications(
    body: ReadAllBody,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    n = await inbox_service.mark_all_read(db, current_user, source=body.source)
    return {"ok": True, "marked": n}


@router.post("/notifications/{notification_id}/read")
async def read_notification(
    notification_id: str,
    body: ReadBody,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    n = await inbox_service.mark_read(db, current_user, notification_id, body.read)
    return {"ok": n is not None}


@router.post("/notifications/{notification_id}/dismiss")
async def dismiss_notification(
    notification_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    n = await inbox_service.dismiss(db, current_user, notification_id)
    return {"ok": n is not None}
