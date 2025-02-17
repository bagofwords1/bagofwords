from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from app.schemas.prompt_schema import PromptCreate, PromptUpdate, PromptResponse
from app.services.prompt_service import PromptService
from app.core.auth import current_user
from app.models.user import User
from app.models.organization import Organization
from app.dependencies import get_async_db, get_current_organization

router = APIRouter()
prompt_service = PromptService()

@router.post("/prompts", response_model=PromptResponse)
async def create_prompt(
    prompt: PromptCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await prompt_service.create_prompt(db, prompt, current_user, organization)

@router.get("/prompts", response_model=List[PromptResponse])
async def read_prompts(
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await prompt_service.get_prompts(db, skip=skip, limit=limit, current_user=current_user, organization=organization)

@router.get("/prompts/{prompt_id}", response_model=PromptResponse)
async def read_prompt(
    prompt_id: int,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    prompt = await prompt_service.get_prompt(db, prompt_id, current_user, organization)
    if prompt is None:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return prompt

@router.put("/prompts/{prompt_id}", response_model=PromptResponse)
async def update_prompt(
    prompt_id: int,
    prompt: PromptUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    updated_prompt = await prompt_service.update_prompt(db, prompt_id, prompt, current_user, organization)
    if updated_prompt is None:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return updated_prompt

@router.delete("/prompts/{prompt_id}", response_model=PromptResponse)
async def delete_prompt(
    prompt_id: int,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    deleted_prompt = await prompt_service.delete_prompt(db, prompt_id, current_user, organization)
    if deleted_prompt is None:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return deleted_prompt