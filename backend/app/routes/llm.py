from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from pydantic import BaseModel

from app.ee.audit.service import audit_service
from app.dependencies import get_async_db, get_current_organization
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.ee.license import require_enterprise
from app.services.llm_service import LLMService
from app.schemas.llm_schema import (
    LLMProviderSchema,
    LLMProviderCreate,
    LLMProviderUpdate,
    LLMProviderTestConnection,
    LLMModelSchema,
    LLMModelCreate,
    LLMModelUpdate,
    LLMModelSchemaWithProvider
)
from app.settings.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["llm"])
llm_service = LLMService()

@router.get("/llm/available_providers", response_model=list[dict])
@requires_permission('manage_llm')
async def get_available_providers(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await llm_service.get_available_providers(db, organization, current_user)

@router.get("/llm/available_models", response_model=list[dict])
@requires_permission('manage_llm')
async def get_available_models(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await llm_service.get_available_models(db, organization, current_user)

@router.post("/llm/test_connection", response_model=dict)
@requires_permission('manage_llm')
async def test_connection(
    provider: LLMProviderTestConnection,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    try:
        return await llm_service.test_connection(db, organization, current_user, provider)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("LLM test_connection route error: %s", e, exc_info=True)
        return {"success": False, "message": str(e)}

@router.get("/llm/providers", response_model=List[LLMProviderSchema])
@requires_permission('manage_llm')
async def get_providers(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get all LLM providers for the organization"""
    return await llm_service.get_providers(db, organization, current_user)

@router.post("/llm/providers", response_model=LLMProviderSchema)
@requires_permission('manage_llm')
async def create_provider(
    provider: LLMProviderCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Create a new custom LLM provider"""
    return await llm_service.create_provider(db, organization, current_user, provider)


@router.put("/llm/providers/{provider_id}", response_model=LLMProviderSchema)
@requires_permission('manage_llm')
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
@requires_permission('manage_llm')
async def delete_provider(
    provider_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Delete a custom provider (preset providers cannot be deleted)"""
    return await llm_service.delete_provider(db, organization, current_user, provider_id)

@router.get("/llm/models", response_model=List[LLMModelSchemaWithProvider])
async def get_models(
    is_enabled: bool = None,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get all LLM models, optionally filtered by status"""
    return await llm_service.get_models(db, organization, current_user, is_enabled)

@router.post("/llm/models", response_model=LLMModelSchema)
@requires_permission('manage_llm')
async def create_model(
    model: LLMModelCreate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Create a new custom model"""
    created = await llm_service.create_model(db, organization, current_user, model)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="llm_model.created",
            user_id=current_user.id, resource_type="llm_model",
            resource_id=str(getattr(created, "id", "") or ""),
            details={"name": getattr(model, "model_id", None) or getattr(model, "name", None)},
            request=request,
        )
    except Exception:
        pass
    return created

@router.patch("/llm/models/{model_id}")
@requires_permission('manage_llm')
async def update_model(
    model_id: str,
    model: LLMModelUpdate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Update model settings"""
    updated = await llm_service.update_model(db, organization, current_user, model_id, model)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="llm_model.updated",
            user_id=current_user.id, resource_type="llm_model", resource_id=str(model_id),
            details={"fields": list(model.dict(exclude_unset=True).keys())},
            request=request,
        )
    except Exception:
        pass
    return updated

@router.delete("/llm/models/{model_id}")
@requires_permission('manage_llm')
async def delete_model(
    model_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Delete a custom model (preset models cannot be deleted)"""
    result = await llm_service.delete_model(db, organization, current_user, model_id)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="llm_model.deleted",
            user_id=current_user.id, resource_type="llm_model", resource_id=str(model_id),
            request=request,
        )
    except Exception:
        pass
    return result

@router.post("/llm/providers/{provider_id}/toggle")
@requires_permission('manage_llm')
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
@requires_permission('manage_llm')
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
@requires_permission('manage_llm')
async def set_default_model(
    model_id: str,
    small: bool = False,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Set a model as the default model for the organization. Use small=true for small default."""
    return await llm_service.set_default_model(db, current_user, organization, model_id, small=small)


# ── Per-model access control (Enterprise) ────────────────────────────────

class ModelAccessAdd(BaseModel):
    principal_type: str  # "user" | "group" | "role"
    principal_id: str


class ModelRestrictionUpdate(BaseModel):
    is_restricted: bool


@router.get("/llm/model-access/by-principal")
@require_enterprise(feature="llm_access_control")
@requires_permission('manage_llm')
async def list_models_for_principal(
    principal_type: str,
    principal_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """List restricted models with whether a principal (role/group/user) can use each."""
    return await llm_service.list_models_for_principal(
        db, organization, current_user, principal_type, principal_id
    )


@router.get("/llm/models/{model_id}/access")
@require_enterprise(feature="llm_access_control")
@requires_permission('manage_llm')
async def get_model_access(
    model_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get the restriction state and granted principals for a model."""
    return await llm_service.get_model_access(db, organization, current_user, model_id)


@router.put("/llm/models/{model_id}/restricted")
@require_enterprise(feature="llm_access_control")
@requires_permission('manage_llm')
async def set_model_restricted(
    model_id: str,
    payload: ModelRestrictionUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Mark a model as access-restricted (or open it back up)."""
    return await llm_service.set_model_restricted(db, organization, current_user, model_id, payload.is_restricted)


@router.post("/llm/models/{model_id}/access")
@require_enterprise(feature="llm_access_control")
@requires_permission('manage_llm')
async def add_model_access(
    model_id: str,
    payload: ModelAccessAdd,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Grant a user/group/role access to a restricted model."""
    return await llm_service.add_model_access(
        db, organization, current_user, model_id, payload.principal_type, payload.principal_id
    )


@router.delete("/llm/models/{model_id}/access/{grant_id}")
@require_enterprise(feature="llm_access_control")
@requires_permission('manage_llm')
async def remove_model_access(
    model_id: str,
    grant_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Revoke a model access grant."""
    return await llm_service.remove_model_access(db, organization, current_user, model_id, grant_id)
