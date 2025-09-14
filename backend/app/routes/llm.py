from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from app.dependencies import get_async_db, get_current_organization
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.services.llm_service import LLMService
from app.schemas.llm_schema import (
    LLMProviderSchema,
    LLMProviderCreate,
    LLMProviderUpdate,
    LLMModelSchema,
    LLMModelCreate,
    LLMModelUpdate,
    LLMModelSchemaWithProvider
)

router = APIRouter(tags=["llm"])
llm_service = LLMService()

@router.get("/llm/available_providers", response_model=list[dict])
@requires_permission('view_llm_settings')
async def get_available_providers(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await llm_service.get_available_providers(db, organization, current_user)

@router.get("/llm/available_models", response_model=list[dict])
@requires_permission('view_llm_settings')
async def get_available_models(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await llm_service.get_available_models(db, organization, current_user)

@router.post("/llm/test_connection", response_model=dict)
@requires_permission('view_llm_settings')
async def test_connection(
    provider: LLMProviderCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await llm_service.test_connection(db, organization, current_user, provider)

@router.get("/llm/providers", response_model=List[LLMProviderSchema])
@requires_permission('view_llm_settings')
async def get_providers(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get all LLM providers for the organization"""
    return await llm_service.get_providers(db, organization, current_user)

@router.post("/llm/providers", response_model=LLMProviderSchema)
@requires_permission('manage_llm_settings')
async def create_provider(
    provider: LLMProviderCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Create a new custom LLM provider"""
    return await llm_service.create_provider(db, organization, current_user, provider)


@router.put("/llm/providers/{provider_id}", response_model=LLMProviderSchema)
@requires_permission('manage_llm_settings')
async def update_provider(
    provider_id: str,
    provider: LLMProviderUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Update provider settings"""
    return await llm_service.update_provider(db, organization, current_user, provider_id, provider)

@router.delete("/llm/providers/{provider_id}")
@requires_permission('manage_llm_settings')
async def delete_provider(
    provider_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Delete a custom provider (preset providers cannot be deleted)"""
    return await llm_service.delete_provider(db, organization, current_user, provider_id)

@router.get("/llm/models", response_model=List[LLMModelSchemaWithProvider])
@requires_permission('view_llm_settings')
async def get_models(
    is_enabled: bool = None,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get all LLM models, optionally filtered by status"""
    return await llm_service.get_models(db, organization, current_user, is_enabled)

@router.post("/llm/models", response_model=LLMModelSchema)
@requires_permission('manage_llm_settings')
async def create_model(
    model: LLMModelCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Create a new custom model"""
    return await llm_service.create_model(db, organization, current_user, model)

@router.patch("/llm/models/{model_id}")
@requires_permission('manage_llm_settings')
async def update_model(
    model_id: str,
    model: LLMModelUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Update model settings"""
    return await llm_service.update_model(db, organization, current_user, model_id, model)

@router.delete("/llm/models/{model_id}")
@requires_permission('manage_llm_settings')
async def delete_model(
    model_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Delete a custom model (preset models cannot be deleted)"""
    return await llm_service.delete_model(db, organization, current_user, model_id)

@router.post("/llm/providers/{provider_id}/toggle")
@requires_permission('manage_llm_settings')
async def toggle_provider(
    provider_id: str,
    enabled: bool,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Enable/disable a provider"""
    return await llm_service.toggle_provider(db, organization, current_user, provider_id, enabled)

@router.post("/llm/models/{model_id}/toggle")
@requires_permission('manage_llm_settings')
async def toggle_model(
    model_id: str,
    enabled: bool,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Enable/disable a model"""
    return await llm_service.toggle_model(db, organization, current_user, model_id, enabled)

@router.post("/llm/models/{model_id}/set_default")
@requires_permission('manage_llm_settings')
async def set_default_model(
    model_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Set a model as the default model for the organization"""
    return await llm_service.set_default_model(db, current_user, organization, model_id)
