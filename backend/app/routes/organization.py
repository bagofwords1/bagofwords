from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID
from app.dependencies import get_async_db
from app.services.organization_service import OrganizationService
from app.schemas.organization_schema import OrganizationCreate, OrganizationSchema, OrganizationAndRoleSchema
from app.schemas.organization_schema import MembershipCreate, MembershipSchema, MembershipUpdate
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from typing import List
from app.dependencies import get_current_organization
from app.core.permissions_decorator import requires_permission

router = APIRouter()
organization_service = OrganizationService()

@router.post("/organizations", response_model=OrganizationSchema)
async def create_organization(organization: OrganizationCreate, db: AsyncSession = Depends(get_async_db), current_user: User = Depends(current_user)):
    return await organization_service.create_organization(db, organization, current_user)

@router.post("/organizations/{organization_id}/members", response_model=MembershipSchema)
@requires_permission('add_organization_members')
async def add_member(organization_id: str, membership: MembershipCreate, organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db)):
    membership.organization_id = organization_id
    return await organization_service.add_member(db, membership, current_user.id)

@router.get("/organizations/{organization_id}/members", response_model=List[MembershipSchema])
@requires_permission('view_organization_members')
async def get_members(organization_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    return await organization_service.get_members(db, organization, current_user)

@router.delete("/organizations/{organization_id}/members/{membership_id}", status_code=204)
@requires_permission('remove_organization_members')
async def remove_member(organization_id: str, membership_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await organization_service.remove_member(db, organization_id, membership_id, current_user, organization)

@router.put("/organizations/{organization_id}/members/{membership_id}", response_model=MembershipSchema)
@requires_permission('update_organization_members')
async def update_member(organization_id: str, membership_id: str, membership: MembershipUpdate, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await organization_service.update_member(db, membership_id, organization_id, membership, current_user, organization)

@router.get("/organizations", response_model=List[OrganizationAndRoleSchema])
async def get_organizations(db: AsyncSession = Depends(get_async_db), current_user: User = Depends(current_user)):
    return await organization_service.get_user_organizations(db, current_user)
