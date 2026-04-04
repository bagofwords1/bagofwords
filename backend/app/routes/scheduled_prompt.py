from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.models.report import Report
from app.models.user import User
from app.models.organization import Organization
from app.services.scheduled_prompt_service import scheduled_prompt_service
from app.schemas.scheduled_prompt_schema import (
    ScheduledPromptCreate,
    ScheduledPromptUpdate,
    ScheduledPromptSchema,
)

router = APIRouter()


@router.post("/reports/{report_id}/scheduled-prompts", response_model=ScheduledPromptSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def create_scheduled_prompt(
    report_id: str,
    body: ScheduledPromptCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await scheduled_prompt_service.create_scheduled_prompt(db, report_id, body, current_user, organization)


@router.get("/reports/{report_id}/scheduled-prompts", response_model=List[ScheduledPromptSchema])
@requires_permission('view_reports', model=Report, owner_only=True)
async def list_scheduled_prompts(
    report_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await scheduled_prompt_service.list_scheduled_prompts(db, report_id)


@router.put("/reports/{report_id}/scheduled-prompts/{sp_id}", response_model=ScheduledPromptSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def update_scheduled_prompt(
    report_id: str,
    sp_id: str,
    body: ScheduledPromptUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await scheduled_prompt_service.update_scheduled_prompt(db, sp_id, body, current_user, organization)


@router.delete("/reports/{report_id}/scheduled-prompts/{sp_id}", status_code=204)
@requires_permission('update_reports', model=Report, owner_only=True)
async def delete_scheduled_prompt(
    report_id: str,
    sp_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    await scheduled_prompt_service.delete_scheduled_prompt(db, sp_id, current_user, organization)


@router.post("/reports/{report_id}/scheduled-prompts/{sp_id}/trigger", status_code=200)
@requires_permission('update_reports', model=Report, owner_only=True)
async def trigger_scheduled_prompt(
    report_id: str,
    sp_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Manually trigger a scheduled prompt execution (for testing / on-demand runs)."""
    await scheduled_prompt_service.scheduled_run_prompt(sp_id)
    return {"status": "triggered", "scheduled_prompt_id": sp_id}
