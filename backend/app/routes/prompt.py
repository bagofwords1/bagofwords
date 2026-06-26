from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.schemas.prompt_schema import (
    PromptCreate, PromptUpdate, PromptResponse, PromptListResponse,
)
from app.services.prompt_service import prompt_service
from app.core.auth import current_user
from app.models.user import User
from app.models.organization import Organization
from app.dependencies import get_async_db, get_current_organization

router = APIRouter(tags=["prompts"])


@router.get("/prompts", response_model=PromptListResponse)
async def list_prompts(
    category: Optional[str] = None,
    starters_only: bool = False,
    data_source_id: Optional[str] = None,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await prompt_service.list_prompts(
        db, current_user, organization,
        category=category, starters_only=starters_only, data_source_id=data_source_id,
    )


@router.get("/prompts/{prompt_id}", response_model=PromptResponse)
async def get_prompt(
    prompt_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await prompt_service.get_prompt_response(db, prompt_id, current_user, organization)


@router.post("/prompts", response_model=PromptResponse)
async def create_prompt(
    data: PromptCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    p = await prompt_service.create_prompt(db, data, current_user, organization)
    return await prompt_service.get_prompt_response(db, p.id, current_user, organization)


@router.put("/prompts/{prompt_id}", response_model=PromptResponse)
async def update_prompt(
    prompt_id: str,
    data: PromptUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    p = await prompt_service.update_prompt(db, prompt_id, data, current_user, organization)
    return await prompt_service.get_prompt_response(db, p.id, current_user, organization)


@router.delete("/prompts/{prompt_id}")
async def delete_prompt(
    prompt_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    await prompt_service.delete_prompt(db, prompt_id, current_user, organization)
    return {"ok": True}
