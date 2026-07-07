"""Standalone trigger routes (user-owned webhooks that spawn sessions).

CRUD is strictly owner-scoped: users see and manage ONLY their own triggers
(the service returns 404 for anyone else's — no existence leak). Identity is
preset at creation: every delivery runs as the trigger's creator with their
agent access, model access, and quota. See docs/design/agent-triggers.md.
"""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.models.user import User
from app.models.organization import Organization
from app.services.webhook_service import webhook_service
from app.ee.audit.service import audit_service
from app.schemas.webhook_schema import (
    TriggerCreate,
    TriggerUpdate,
    WebhookSchema,
    TriggerRunListResponse,
)

router = APIRouter()


@router.get("/triggers", response_model=List[WebhookSchema])
async def list_triggers(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await webhook_service.list_triggers(db, current_user, organization)


@router.post("/triggers", response_model=WebhookSchema)
async def create_trigger(
    body: TriggerCreate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    wh = await webhook_service.create_trigger(db, body, current_user, organization)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="trigger.created",
            user_id=current_user.id, resource_type="webhook", resource_id=wh.id,
            details={"source": wh.source, "mode": wh.mode,
                     "data_source_ids": [d.id for d in wh.data_sources]},
            request=request,
        )
    except Exception:
        pass
    return wh


@router.put("/triggers/{trigger_id}", response_model=WebhookSchema)
async def update_trigger(
    trigger_id: str,
    body: TriggerUpdate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    wh = await webhook_service.update_trigger(db, trigger_id, body, current_user, organization)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="trigger.updated",
            user_id=current_user.id, resource_type="webhook", resource_id=trigger_id,
            details={"fields": list(body.model_dump(exclude_unset=True).keys())},
            request=request,
        )
    except Exception:
        pass
    return wh


@router.post("/triggers/{trigger_id}/rotate", response_model=WebhookSchema)
async def rotate_trigger_secret(
    trigger_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    wh = await webhook_service.rotate_trigger_secret(db, trigger_id, current_user)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="trigger.secret_rotated",
            user_id=current_user.id, resource_type="webhook", resource_id=trigger_id,
            details={}, request=request,
        )
    except Exception:
        pass
    return wh


@router.delete("/triggers/{trigger_id}", status_code=204)
async def delete_trigger(
    trigger_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    await webhook_service.delete_trigger(db, trigger_id, current_user)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="trigger.deleted",
            user_id=current_user.id, resource_type="webhook", resource_id=trigger_id,
            details={}, request=request,
        )
    except Exception:
        pass


@router.get("/triggers/{trigger_id}/runs", response_model=TriggerRunListResponse)
async def get_trigger_runs(
    trigger_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await webhook_service.get_trigger_runs(db, trigger_id, current_user, page=page, limit=limit)
