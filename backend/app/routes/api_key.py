from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.models.user import User
from app.models.organization import Organization
from app.schemas.api_key_schema import ApiKeyCreate, ApiKeyResponse, ApiKeyCreated
from app.services.api_key_service import ApiKeyService

router = APIRouter(prefix="/api_keys", tags=["api_keys"])
api_key_service = ApiKeyService()


@router.post("", response_model=ApiKeyCreated)
async def create_api_key(
    data: ApiKeyCreate,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """Create a new API key for the current user within the current organization.
    
    The full key is only returned once upon creation. Store it securely.
    """
    return await api_key_service.create_api_key(db, data, user, organization)


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
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
):
    """Revoke an API key."""
    await api_key_service.delete_api_key(db, key_id, user)
    return {"message": "API key revoked"}


