"""Review feed API — admin-facing, actionable items about agents.

Visibility and actions are gated to users with ``manage`` on the item's agent
(full admins see everything). The service enforces this per item.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.dependencies import get_async_db, get_current_organization
from app.models.organization import Organization
from app.models.user import User
from app.errors import AppError, ErrorCode
from app.services.review_service import review_service

router = APIRouter(tags=["review"])


class ReadBody(BaseModel):
    read: bool = True


class ResolveBody(BaseModel):
    action_id: str
    params: Optional[Dict[str, Any]] = None


class ReadAllBody(BaseModel):
    folder_id: Optional[str] = None
    agent_id: Optional[str] = None


@router.get("/review")
async def list_review(
    folder_id: Optional[str] = Query(None),
    agent_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="comma-separated"),
    type: Optional[str] = Query(None, description="comma-separated"),
    severity: Optional[str] = Query(None, description="comma-separated"),
    search: Optional[str] = Query(None),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    def _split(v):
        return [s for s in (v.split(",") if v else []) if s] or None
    return await review_service.list_items(
        db, organization, current_user,
        agent_id=agent_id, folder_id=folder_id,
        statuses=_split(status),
        types=_split(type),
        severities=_split(severity),
        search=search,
    )


@router.get("/review/count")
async def count_review(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await review_service.count_open(db, organization, current_user)


@router.post("/review/read-all")
async def read_all_review(
    body: ReadAllBody,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    n = await review_service.mark_all_read(db, organization, current_user, agent_id=body.agent_id, folder_id=body.folder_id)
    return {"ok": True, "marked": n}


@router.post("/review/{item_id}/read")
async def read_review(
    item_id: str,
    body: ReadBody,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    item = await review_service.mark_read(db, organization, current_user, item_id, body.read)
    return {"ok": item is not None}


@router.post("/review/{item_id}/dismiss")
async def dismiss_review(
    item_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    item = await review_service.dismiss(db, organization, current_user, item_id)
    return {"ok": item is not None}


@router.get("/review/{item_id}/training-context")
async def review_training_context(
    item_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    ctx = await review_service.training_context(db, organization, current_user, item_id)
    if ctx is None:
        raise AppError.not_found(ErrorCode.REVIEW_ITEM_NOT_FOUND, "Review item not found")
    return ctx


@router.post("/review/{item_id}/resolve")
async def resolve_review(
    item_id: str,
    body: ResolveBody,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await review_service.resolve(db, organization, current_user, item_id, body.action_id, body.params)
