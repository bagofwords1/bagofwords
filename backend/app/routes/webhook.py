from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.models.report import Report
from app.models.user import User
from app.models.organization import Organization
from app.services.webhook_service import webhook_service
from app.services.webhook_adapters.factory import WebhookAdapterFactory
from app.schemas.webhook_schema import WebhookCreate, WebhookUpdate, WebhookSchema

router = APIRouter()


@router.get("/webhooks/sources")
async def list_sources(current_user: User = Depends(current_user)):
    """Available webhook source presets (drives the modal dropdown + icons)."""
    presets = {
        "github": {"label": "GitHub", "default_auth_mode": "hmac"},
        "jira": {"label": "Jira", "default_auth_mode": "token"},
        "generic": {"label": "Generic", "default_auth_mode": "hmac"},
    }
    return [{"source": s, **presets.get(s, {"label": s, "default_auth_mode": "hmac"})}
            for s in WebhookAdapterFactory.sources()]


@router.post("/reports/{report_id}/webhooks", response_model=WebhookSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def create_webhook(
    report_id: str,
    body: WebhookCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await webhook_service.create_webhook(db, report_id, body, current_user, organization)


@router.get("/reports/{report_id}/webhooks", response_model=List[WebhookSchema])
@requires_permission('view_reports', model=Report, owner_only=True)
async def list_webhooks(
    report_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await webhook_service.list_webhooks(db, report_id)


@router.put("/reports/{report_id}/webhooks/{webhook_id}", response_model=WebhookSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def update_webhook(
    report_id: str,
    webhook_id: str,
    body: WebhookUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await webhook_service.update_webhook(db, webhook_id, body)


@router.post("/reports/{report_id}/webhooks/{webhook_id}/rotate", response_model=WebhookSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def rotate_webhook_secret(
    report_id: str,
    webhook_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await webhook_service.rotate_secret(db, webhook_id)


@router.delete("/reports/{report_id}/webhooks/{webhook_id}", status_code=204)
@requires_permission('update_reports', model=Report, owner_only=True)
async def delete_webhook(
    report_id: str,
    webhook_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    await webhook_service.delete_webhook(db, webhook_id)
