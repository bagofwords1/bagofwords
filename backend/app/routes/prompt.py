from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.schemas.prompt_catalog_schema import (
    PromptCatalogCreate, PromptCatalogUpdate, PromptCatalogResponse, PromptListResponse,
    SubscribeRequest, AssignRequest, AssignResponse, RunNowResponse,
)
from app.schemas.scheduled_prompt_schema import ScheduledPromptSchema
from app.services.prompt_catalog_service import prompt_catalog_service
from app.core.auth import current_user
from app.models.user import User
from app.models.organization import Organization
from app.dependencies import get_async_db, get_current_organization

router = APIRouter(tags=["prompts"])


@router.get("/prompts", response_model=PromptListResponse)
async def list_prompts(
    sort: str = Query('recent', description="'recent' | 'top'"),
    category: Optional[str] = None,
    starters_only: bool = False,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await prompt_catalog_service.list_prompts(
        db, current_user, organization, sort=sort, category=category, starters_only=starters_only,
    )


@router.get("/prompts/{prompt_id}", response_model=PromptCatalogResponse)
async def get_prompt(
    prompt_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await prompt_catalog_service.get_prompt_response(db, prompt_id, current_user, organization)


@router.post("/prompts", response_model=PromptCatalogResponse)
async def create_prompt(
    data: PromptCatalogCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    p = await prompt_catalog_service.create_prompt(db, data, current_user, organization)
    return await prompt_catalog_service.get_prompt_response(db, p.id, current_user, organization)


@router.put("/prompts/{prompt_id}", response_model=PromptCatalogResponse)
async def update_prompt(
    prompt_id: str,
    data: PromptCatalogUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    p = await prompt_catalog_service.update_prompt(db, prompt_id, data, current_user, organization)
    return await prompt_catalog_service.get_prompt_response(db, p.id, current_user, organization)


@router.delete("/prompts/{prompt_id}")
async def delete_prompt(
    prompt_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    await prompt_catalog_service.delete_prompt(db, prompt_id, current_user, organization)
    return {"ok": True}


@router.post("/prompts/{prompt_id}/run", response_model=RunNowResponse)
async def run_prompt_now(
    prompt_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await prompt_catalog_service.run_now(db, prompt_id, current_user, organization)


@router.post("/prompts/{prompt_id}/subscribe", response_model=ScheduledPromptSchema)
async def subscribe_prompt(
    prompt_id: str,
    data: SubscribeRequest,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await prompt_catalog_service.subscribe(db, prompt_id, data, current_user, organization)


@router.post("/prompts/{prompt_id}/assign", response_model=AssignResponse)
async def assign_prompt(
    prompt_id: str,
    data: AssignRequest,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await prompt_catalog_service.assign(db, prompt_id, data, current_user, organization)
