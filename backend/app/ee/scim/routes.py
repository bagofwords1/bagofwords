# SCIM 2.0 Routes
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details

from typing import Optional
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.ee.license import require_enterprise
from app.ee.scim.auth import scim_auth
from app.ee.scim.service import ScimTokenService, ScimUserService
from app.ee.scim.schemas import (
    ScimUserCreate, ScimPatchOp, ScimListResponse, ScimUser,
    ScimTokenCreate, ScimTokenResponse, ScimTokenCreated,
)
from app.ee.scim.constants import SERVICE_PROVIDER_CONFIG, SCHEMAS, RESOURCE_TYPES
from app.models.user import User
from app.models.organization import Organization

scim_token_service = ScimTokenService()
scim_user_service = ScimUserService()


class SCIMResponse(JSONResponse):
    media_type = "application/scim+json"


# --- SCIM Provisioning Router (Bearer token auth, mounted at /scim/v2) ---

scim_router = APIRouter(prefix="/scim/v2", tags=["scim"], default_response_class=SCIMResponse)


# Discovery endpoints

@scim_router.get("/ServiceProviderConfig")
async def get_service_provider_config(
    organization: Organization = Depends(scim_auth),
):
    return SERVICE_PROVIDER_CONFIG


@scim_router.get("/Schemas")
async def get_schemas(
    organization: Organization = Depends(scim_auth),
):
    return SCHEMAS


@scim_router.get("/ResourceTypes")
async def get_resource_types(
    organization: Organization = Depends(scim_auth),
):
    return RESOURCE_TYPES


# User endpoints

@scim_router.get("/Users", response_model=ScimListResponse)
async def list_users(
    request: Request,
    filter: Optional[str] = Query(None),
    startIndex: int = Query(1, ge=1),
    count: int = Query(100, ge=1, le=100),
    organization: Organization = Depends(scim_auth),
    db: AsyncSession = Depends(get_async_db),
):
    base_url = str(request.base_url).rstrip("/")
    return await scim_user_service.list_users(
        db=db,
        organization_id=str(organization.id),
        filter_str=filter,
        start_index=startIndex,
        count=count,
        base_url=base_url,
    )


@scim_router.post("/Users", response_model=ScimUser, status_code=201)
async def create_user(
    request: Request,
    data: ScimUserCreate,
    organization: Organization = Depends(scim_auth),
    db: AsyncSession = Depends(get_async_db),
):
    base_url = str(request.base_url).rstrip("/")
    return await scim_user_service.create_user(
        db=db,
        organization_id=str(organization.id),
        data=data,
        base_url=base_url,
    )


@scim_router.get("/Users/{user_id}", response_model=ScimUser)
async def get_user(
    request: Request,
    user_id: str,
    organization: Organization = Depends(scim_auth),
    db: AsyncSession = Depends(get_async_db),
):
    base_url = str(request.base_url).rstrip("/")
    return await scim_user_service.get_user(
        db=db,
        organization_id=str(organization.id),
        user_id=user_id,
        base_url=base_url,
    )


@scim_router.put("/Users/{user_id}", response_model=ScimUser)
async def update_user(
    request: Request,
    user_id: str,
    data: ScimUserCreate,
    organization: Organization = Depends(scim_auth),
    db: AsyncSession = Depends(get_async_db),
):
    base_url = str(request.base_url).rstrip("/")
    return await scim_user_service.update_user(
        db=db,
        organization_id=str(organization.id),
        user_id=user_id,
        data=data,
        base_url=base_url,
    )


@scim_router.patch("/Users/{user_id}", response_model=ScimUser)
async def patch_user(
    request: Request,
    user_id: str,
    data: ScimPatchOp,
    organization: Organization = Depends(scim_auth),
    db: AsyncSession = Depends(get_async_db),
):
    base_url = str(request.base_url).rstrip("/")
    return await scim_user_service.patch_user(
        db=db,
        organization_id=str(organization.id),
        user_id=user_id,
        patch=data,
        base_url=base_url,
    )


@scim_router.delete("/Users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    organization: Organization = Depends(scim_auth),
    db: AsyncSession = Depends(get_async_db),
):
    await scim_user_service.delete_user(
        db=db,
        organization_id=str(organization.id),
        user_id=user_id,
    )
    return Response(status_code=204)


# --- Token Management Router (JWT auth, mounted under /api/enterprise/scim) ---

scim_admin_router = APIRouter(prefix="/enterprise/scim", tags=["enterprise", "scim"])


@scim_admin_router.post("/tokens", response_model=ScimTokenCreated, status_code=201)
@require_enterprise(feature="scim")
@requires_permission("manage_identity_providers")
async def create_scim_token(
    data: ScimTokenCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await scim_token_service.create_token(
        db=db,
        organization=organization,
        user_id=str(current_user.id),
        data=data,
    )


@scim_admin_router.get("/tokens", response_model=list[ScimTokenResponse])
@require_enterprise(feature="scim")
@requires_permission("manage_identity_providers")
async def list_scim_tokens(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await scim_token_service.list_tokens(
        db=db,
        organization_id=str(organization.id),
    )


@scim_admin_router.delete("/tokens/{token_id}", status_code=204)
@require_enterprise(feature="scim")
@requires_permission("manage_identity_providers")
async def revoke_scim_token(
    token_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    await scim_token_service.revoke_token(
        db=db,
        organization_id=str(organization.id),
        token_id=token_id,
    )
    return Response(status_code=204)
