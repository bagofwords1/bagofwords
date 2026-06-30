from typing import List

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user, forbid_service_account_principal
from app.core.permissions_decorator import requires_permission
from app.models.user import User
from app.models.organization import Organization
from app.schemas.service_account_schema import (
    ServiceAccountCreate, ServiceAccountUpdate, ServiceAccountResponse,
    ServiceAccountDetail, ServiceAccountKeyCreate,
)
from app.schemas.api_key_schema import ApiKeyCreated
from app.services.service_account_service import ServiceAccountService
from app.ee.audit.service import audit_service

# Service accounts are a core (non-EE) capability: any full admin (or a custom
# role holding `manage_service_accounts`) can CRUD them. A request
# authenticated AS a service account is blocked here (no self-replication).
router = APIRouter(
    prefix="/service_accounts",
    tags=["service_accounts"],
    dependencies=[Depends(forbid_service_account_principal)],
)
service = ServiceAccountService()


@router.get("", response_model=List[ServiceAccountResponse])
@requires_permission("manage_service_accounts")
async def list_service_accounts(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    return await service.list_service_accounts(db, organization)


@router.post("", response_model=ServiceAccountResponse)
@requires_permission("manage_service_accounts")
async def create_service_account(
    data: ServiceAccountCreate,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    result = await service.create_service_account(db, data, current_user, organization)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="service_account.created",
            user_id=current_user.id, resource_type="service_account", resource_id=result.id,
            details={"name": data.name}, request=request,
        )
    except Exception:
        pass
    return result


@router.get("/{sa_id}", response_model=ServiceAccountDetail)
@requires_permission("manage_service_accounts")
async def get_service_account(
    sa_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    return await service.get_service_account(db, organization, sa_id)


@router.patch("/{sa_id}", response_model=ServiceAccountResponse)
@requires_permission("manage_service_accounts")
async def update_service_account(
    sa_id: str,
    data: ServiceAccountUpdate,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    result = await service.update_service_account(db, organization, sa_id, data, current_user)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="service_account.updated",
            user_id=current_user.id, resource_type="service_account", resource_id=sa_id,
            details=data.model_dump(exclude_none=True), request=request,
        )
    except Exception:
        pass
    return result


@router.delete("/{sa_id}")
@requires_permission("manage_service_accounts")
async def delete_service_account(
    sa_id: str,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    await service.delete_service_account(db, organization, sa_id)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="service_account.deleted",
            user_id=current_user.id, resource_type="service_account", resource_id=sa_id,
            request=request,
        )
    except Exception:
        pass
    return {"message": "Service account deleted"}


@router.post("/{sa_id}/keys", response_model=ApiKeyCreated)
@requires_permission("manage_service_accounts")
async def create_service_account_key(
    sa_id: str,
    data: ServiceAccountKeyCreate,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    result = await service.create_key(db, organization, sa_id, data)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="service_account.key_created",
            user_id=current_user.id, resource_type="service_account", resource_id=sa_id,
            details={"key_id": result.id, "name": data.name}, request=request,
        )
    except Exception:
        pass
    return result


@router.delete("/{sa_id}/keys/{key_id}")
@requires_permission("manage_service_accounts")
async def revoke_service_account_key(
    sa_id: str,
    key_id: str,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    await service.revoke_key(db, organization, sa_id, key_id)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="service_account.key_revoked",
            user_id=current_user.id, resource_type="service_account", resource_id=sa_id,
            details={"key_id": key_id}, request=request,
        )
    except Exception:
        pass
    return {"message": "API key revoked"}
