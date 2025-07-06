from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.organization import Organization
from app.models.membership import Membership
from app.schemas.organization_schema import OrganizationCreate, OrganizationSchema, OrganizationAndRoleSchema
from app.schemas.organization_schema import MembershipCreate, MembershipSchema, MembershipUpdate
from app.schemas.organization_settings_schema import OrganizationSettingsCreate
from app.services.organization_settings_service import OrganizationSettingsService
from uuid import UUID
from app.models.user import User
from typing import List
from sqlalchemy.orm import selectinload
from fastapi import HTTPException
from sqlalchemy import delete
from app.services.llm_service import LLMService
from app.settings.config import settings
from fastapi import Request
from fastapi_mail import FastMail, MessageSchema
import asyncio
from typing import Optional
from app.settings.logging_config import get_logger

logger = get_logger(__name__)

class OrganizationService:

    def __init__(self):
        self.llm_service = LLMService()
        self.organization_settings_service = OrganizationSettingsService()
    async def create_organization(self, db: AsyncSession, organization_data: OrganizationCreate, current_user: User) -> OrganizationSchema:

        total_orgs = await db.execute(select(Organization))
        total_orgs = total_orgs.scalars().all().__len__()
        if total_orgs > 0 and not settings.bow_config.features.allow_multiple_organizations:
            raise HTTPException(status_code=400, detail="You cannot create more than one organization")
        
        organization = Organization(**organization_data.dict())
        db.add(organization)
        await db.commit()
        await db.refresh(organization)

        await self.organization_settings_service.create_default_settings(db, organization, current_user)
        await self.add_member(db, MembershipCreate(role="admin", user_id=current_user.id, organization_id=organization.id), current_user)
        await self.llm_service.set_default_models_from_config(db, organization, current_user)

        return OrganizationSchema.from_orm(organization)

    async def get_organization(self, db: AsyncSession, organization_id: str, current_user: User) -> OrganizationSchema:
        result = await db.execute(select(Organization).where(Organization.id == organization_id))
        return result.scalar_one_or_none()
    
    async def get_members(self, db: AsyncSession, organization: Organization, current_user: User) -> List[MembershipSchema]:
        result = await db.execute(
            select(Membership)
            .options(selectinload(Membership.user))
            .where(Membership.organization_id == organization.id)
        )
        result = result.scalars().all()
        return [MembershipSchema.from_orm(membership) for membership in result]
    
    async def get_member(self, db: AsyncSession, membership_id: str, organization_id: str, current_user: User) -> MembershipSchema:
        result = await db.execute(
            select(Membership)
            .options(selectinload(Membership.user))
            .where(Membership.id == membership_id, Membership.organization_id == organization_id)
        )
        return result.scalar_one_or_none()
    

    async def add_member(self, db: AsyncSession, membership_data: MembershipCreate, current_user: User) -> MembershipSchema:
        #check if email is already a user
        # if it is, add user_id to membership and remove email
        # then, check if user (or email) already maps to a membership in this organization
        # if it does, raise an error
        membership_exists = await self._is_email_already_in_organization(db, membership_data.email, membership_data.organization_id)
        if membership_exists:
            raise HTTPException(status_code=400, detail="Already a member with this email")
        
        user = await db.execute(select(User).where(User.email == membership_data.email))
        user = user.scalar_one_or_none()

        # Store the email for invitation before potentially setting it to None
        invitation_email = membership_data.email

        if user:
            membership_data.user_id = user.id
            membership_data.email = None

        membership = Membership(**membership_data.dict())

        db.add(membership)
        await db.commit()
        await db.refresh(membership)
        
        # Reload the membership with the user relationship
        result = await db.execute(
            select(Membership)
            .options(selectinload(Membership.user))
            .where(Membership.id == membership.id)
        )
        membership_with_user = result.scalar_one()
        
        # Send invitation email if email client is configured
        if hasattr(settings, 'email_client') and settings.email_client and invitation_email:
            await self._send_invitation_email(membership_with_user, invitation_email)
            
        return MembershipSchema.from_orm(membership_with_user)
    
    async def get_user_organizations(self, db: AsyncSession, current_user: User) -> List[OrganizationAndRoleSchema]:
        result = await db.execute(
            select(Organization, Membership.role)
            .join(Membership)
            .where(Membership.user_id == current_user.id)
        )
        results = result.all()
        return [
            OrganizationAndRoleSchema(
                id=org.id,
                name=org.name,
                description=org.description,
                role=role
            ) 
            for org, role in results
        ]


    async def remove_member(self, db: AsyncSession, organization_id, membership_id: str, current_user: User, organization: Organization) -> None:
        # check fi the selected membership is admin, and if it is the only admin
        membership = await self.get_member(db, membership_id, organization_id, current_user)
        if not membership:
            raise HTTPException(status_code=404, detail="Membership not found")
        if membership.user_id:
            admin_count = await self._active_admin_count(db, organization, current_user)
            if admin_count == 1 and membership.role == "admin":
                raise HTTPException(status_code=400, detail="You cannot remove the only admin from the organization")

        await db.execute(delete(Membership).where(Membership.id == membership_id))
        await db.commit()
    
    async def update_member(self, db: AsyncSession, membership_id: str, organization_id: str, membership_data: MembershipUpdate, current_user: User, organization: Organization) -> MembershipSchema:
        membership = await self.get_member(db, membership_id, organization_id, current_user)
        if not membership:
            raise HTTPException(status_code=404, detail="Membership not found")
        
        # validate that the membership is not the only active admin in the organization
        if membership.user_id:
            admin_count = await self._active_admin_count(db, organization, current_user)
            if admin_count == 1 and membership.role == "admin":
                raise HTTPException(status_code=400, detail="You cannot update the role of the only active admin in the organization")

        # currently only updating role
        membership.role = membership_data.role
        await db.commit()
        await db.refresh(membership)

        return MembershipSchema.from_orm(membership)
    
    async def _is_email_already_in_organization(self, db: AsyncSession, email: str, organization_id: str) -> bool:
        user = await db.execute(select(User).where(User.email == email))
        user = user.scalar_one_or_none()
        if user:
            membership = await db.execute(select(Membership).where(Membership.user_id == user.id, Membership.organization_id == organization_id))
            membership = membership.scalar_one_or_none()
            return membership 
        
        email_membership = await db.execute(select(Membership).where(Membership.email == email, Membership.organization_id == organization_id))
        email_membership = email_membership.scalar_one_or_none()
        if email_membership:
            return email_membership 
        
        return False
    
    async def _active_admin_count(self, db: AsyncSession, organization: Organization, current_user: User) -> bool:
        admin_memberships = await self.get_members(db, organization, current_user)
        admin_members = [membership for membership in admin_memberships if membership.role == "admin" and membership.user_id is not None]

        return len(admin_members)
    
    async def _send_invitation_email(self, membership: Membership, email: str):
        sign_up_url = settings.bow_config.base_url + "/users/sign-up?email=" + email

        message = MessageSchema(
            subject="You are invited to Bag of words",
            recipients=[email],
            body=f"You have been invited to join an organization on Bag of words. Click to sign up: <br /> {sign_up_url}",
            subtype="html")
        fm = settings.email_client
        logger.info(f"Using email client: {fm}")

        async def send_email():
            logger.info(f"Sending invitation email to: {email}")
            try:
                await fm.send_message(message)
                logger.info(f"Invitation email sent successfully to: {email}")
            except Exception as e:
                logger.error(f"Error sending invitation email: {e}")

        asyncio.create_task(send_email())
