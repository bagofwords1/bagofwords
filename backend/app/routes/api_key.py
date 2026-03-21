from typing import List

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.models.user import User
from app.models.organization import Organization
from app.schemas.api_key_schema import ApiKeyCreate, ApiKeyResponse, ApiKeyCreated
from app.services.api_key_service import ApiKeyService
from app.ee.audit.service import audit_service

router = APIRouter(prefix="/api_keys", tags=["api_keys"])
api_key_service = ApiKeyService()


@router.post("", response_model=ApiKeyCreated)
async def create_api_key(
    data: ApiKeyCreate,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """Create a new API key for the current user within the current organization.

    The full key is only returned once upon creation. Store it securely.
    """
    result = await api_key_service.create_api_key(db, data, user, organization)
    try:
        await audit_service.log(
            db=db,
            organization_id=organization.id,
            action="api_key.created",
            user_id=user.id,
            resource_type="api_key",
            resource_id=result.id,
            details={"name": data.name},
            request=request,
        )
    except Exception:
        pass
    return result


@router.get("", response_model=List[ApiKeyResponse])
async def list_api_keys(
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
):
    """List all API keys for the current user."""
    return await api_key_service.list_api_keys(db, user)


@router.delete("/{key_id}")
async def delete_api_key(
    key_id: str,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """Revoke an API key."""
    await api_key_service.delete_api_key(db, key_id, user)
    try:
        await audit_service.log(
            db=db,
            organization_id=organization.id,
            action="api_key.revoked",
            user_id=user.id,
            resource_type="api_key",
            resource_id=key_id,
            request=request,
        )
    except Exception:
        pass
    return {"message": "API key revoked"}


