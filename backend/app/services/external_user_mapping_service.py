from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from fastapi import HTTPException
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
import secrets
import uuid

from app.models.external_user_mapping import ExternalUserMapping
from app.models.user import User
from app.models.organization import Organization
from app.schemas.external_user_mapping_schema import (
    ExternalUserMappingCreate,
    ExternalUserMappingUpdate,
    ExternalUserMappingSchema
)

class ExternalUserMappingService:
    
    async def create_mapping(
        self, 
        db: AsyncSession, 
        organization: Organization,
        mapping_data: ExternalUserMappingCreate,
        platform_id: str = None
    ) -> ExternalUserMappingSchema:
        """Create a new external user mapping"""
        
        # Check if mapping already exists
        existing_mapping = await self.get_mapping_by_external_id(
            db, organization.id, mapping_data.platform_type, mapping_data.external_user_id
        )
        if existing_mapping:
            raise HTTPException(
                status_code=400, 
                detail="External user mapping already exists"
            )
        
        # Only verify app user if app_user_id is provided
        if mapping_data.app_user_id:
            app_user = await self._verify_app_user(db, mapping_data.app_user_id, organization.id)
        
        # Create mapping
        mapping = ExternalUserMapping(
            organization_id=organization.id,
            platform_id=platform_id,  # Use the provided platform_id
            platform_type=mapping_data.platform_type,
            external_user_id=mapping_data.external_user_id,
            external_email=mapping_data.external_email,
            external_name=mapping_data.external_name,
            app_user_id=mapping_data.app_user_id,
            is_verified=mapping_data.is_verified
        )
        
        db.add(mapping)
        await db.commit()
        await db.refresh(mapping)
        
        return ExternalUserMappingSchema.from_orm(mapping)
    
    async def get_mappings(
        self, 
        db: AsyncSession, 
        organization: Organization,
        platform_type: Optional[str] = None
    ) -> List[ExternalUserMappingSchema]:
        """Get all external user mappings for an organization"""
        
        stmt = select(ExternalUserMapping).where(
            ExternalUserMapping.organization_id == organization.id
        )
        
        if platform_type:
            stmt = stmt.where(ExternalUserMapping.platform_type == platform_type)
        
        result = await db.execute(stmt)
        mappings = result.scalars().all()
        
        return [ExternalUserMappingSchema.from_orm(mapping) for mapping in mappings]
    
    async def get_mapping_by_external_id(
        self, 
        db: AsyncSession, 
        organization_id: str,
        platform_type: str,
        external_user_id: str
    ) -> Optional[ExternalUserMapping]:
        """Get mapping by external user ID"""
        
        stmt = select(ExternalUserMapping).where(
            and_(
                ExternalUserMapping.organization_id == organization_id,
                ExternalUserMapping.platform_type == platform_type,
                ExternalUserMapping.external_user_id == external_user_id
            )
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()
    
    async def get_mapping_by_app_user(
        self, 
        db: AsyncSession, 
        organization_id: str,
        platform_type: str,
        app_user_id: str
    ) -> Optional[ExternalUserMapping]:
        """Get mapping by app user ID"""
        
        stmt = select(ExternalUserMapping).where(
            and_(
                ExternalUserMapping.organization_id == organization_id,
                ExternalUserMapping.platform_type == platform_type,
                ExternalUserMapping.app_user_id == app_user_id
            )
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()
    
    async def update_mapping(
        self, 
        db: AsyncSession, 
        mapping_id: str,
        mapping_data: ExternalUserMappingUpdate,
        organization: Organization
    ) -> ExternalUserMappingSchema:
        """Update an external user mapping"""
        
        mapping = await self._get_mapping_by_id(db, mapping_id, organization.id)
        
        # Update fields
        if mapping_data.external_email is not None:
            mapping.external_email = mapping_data.external_email
        if mapping_data.external_name is not None:
            mapping.external_name = mapping_data.external_name
        if mapping_data.is_verified is not None:
            mapping.is_verified = mapping_data.is_verified
            if mapping_data.is_verified:
                mapping.last_verified_at = datetime.utcnow()
        
        await db.commit()
        await db.refresh(mapping)
        
        return ExternalUserMappingSchema.from_orm(mapping)
    
    async def delete_mapping(
        self, 
        db: AsyncSession, 
        mapping_id: str,
        organization: Organization
    ) -> bool:
        """Delete an external user mapping"""
        
        mapping = await self._get_mapping_by_id(db, mapping_id, organization.id)
        
        await db.delete(mapping)
        await db.commit()
        
        return True
    
    async def generate_verification_token(
        self, 
        db: AsyncSession, 
        mapping_id: str,
        organization: Organization
    ) -> str:
        """Generate a verification token for a mapping"""
        
        mapping = await self._get_mapping_by_id(db, mapping_id, organization.id)
        
        # Generate secure token
        token = secrets.token_urlsafe(32)
        mapping.verification_token = token
        mapping.verification_expires_at = datetime.utcnow() + timedelta(days=365)
        
        await db.commit()
        
        return token
    
    async def verify_token(
        self, 
        db: AsyncSession, 
        token: str
    ) -> dict:
        """Verify a verification token"""
        
        stmt = select(ExternalUserMapping).where(
            and_(
                ExternalUserMapping.verification_token == token,
                ExternalUserMapping.verification_expires_at > datetime.utcnow()
            )
        )
        result = await db.execute(stmt)
        mapping = result.scalar_one_or_none()
        
        if not mapping:
            return {"success": False, "error": "Invalid or expired verification token"}
        
        # Mark as verified
        mapping.is_verified = True
        mapping.verification_token = None
        mapping.verification_expires_at = None
        mapping.last_verified_at = datetime.utcnow()
        
        await db.commit()
        
        return {
            "success": True,
            "mapping_id": mapping.id,
            "external_email": mapping.external_email,
            "platform_type": mapping.platform_type
        }
    
    async def find_user_by_email(
        self, 
        db: AsyncSession, 
        organization_id: str,
        email: str
    ) -> Optional[User]:
        """Find app user by email in organization"""
        
        from app.models.membership import Membership
        
        # Join users with memberships to find users in the organization
        stmt = select(User).join(Membership).where(
            and_(
                User.email == email,
                Membership.organization_id == organization_id
            )
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()
    
    async def _get_mapping_by_id(
        self, 
        db: AsyncSession, 
        mapping_id: str,
        organization_id: str
    ) -> ExternalUserMapping:
        """Get mapping by ID with organization check"""
        
        stmt = select(ExternalUserMapping).where(
            and_(
                ExternalUserMapping.id == mapping_id,
                ExternalUserMapping.organization_id == organization_id
            )
        )
        result = await db.execute(stmt)
        mapping = result.scalar_one_or_none()
        
        if not mapping:
            raise HTTPException(status_code=404, detail="External user mapping not found")
        
        return mapping
    
    async def _verify_app_user(
        self, 
        db: AsyncSession, 
        app_user_id: str,
        organization_id: str
    ) -> User:
        """Verify app user exists and belongs to organization"""
        
        from app.models.membership import Membership
        
        # Join users with memberships to find users in the organization
        stmt = select(User).join(Membership).where(
            and_(
                User.id == app_user_id,
                Membership.organization_id == organization_id
            )
        )
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()
        
        if not user:
            raise HTTPException(
                status_code=404, 
                detail="App user not found or not a member of this organization"
            )
        
        return user

    async def get_mapping_by_token(self, db: AsyncSession, token: str) -> Optional[ExternalUserMapping]:
        """Get mapping by verification token"""
        stmt = select(ExternalUserMapping).where(
            and_(
                ExternalUserMapping.verification_token == token,
                ExternalUserMapping.verification_expires_at > datetime.utcnow()
            )
        )
        result = await db.execute(stmt)
        mapping = result.scalar_one_or_none() 
        return mapping

    async def complete_verification(
        self, 
        db: AsyncSession, 
        token: str,
        current_user: User
    ) -> dict:
        """Complete verification by linking to signed-in user"""
        mapping = await self.get_mapping_by_token(db, token)

        if not mapping:
            # Try to find a mapping for this user that is already verified
            # (Optional: you could also look up by current_user.id and platform_type)
            stmt = select(ExternalUserMapping).where(
                and_(
                    ExternalUserMapping.app_user_id == current_user.id,
                    ExternalUserMapping.is_verified == True,
                    ExternalUserMapping.verification_token == None
                )
            )
            result = await db.execute(stmt)
            already_verified = result.scalar_one_or_none()
            if already_verified:
                return {
                    "success": True,
                    "already_verified": True,
                    "message": "Your account is already verified.",
                    "mapping_id": already_verified.id,
                    "external_user_id": already_verified.external_user_id,
                    "platform_type": already_verified.platform_type
                }
            return {"success": False, "error": "Invalid or expired verification token"}
        
        # If mapping is already verified, return a friendly message
        if mapping.is_verified:
            return {
                "success": True,
                "already_verified": True,
                "message": "Your account is already verified.",
                "mapping_id": mapping.id,
                "external_user_id": mapping.external_user_id,
                "platform_type": mapping.platform_type
            }
        
        # Update mapping with user info
        mapping.app_user_id = current_user.id
        mapping.external_email = current_user.email
        mapping.external_name = current_user.name
        mapping.is_verified = True
        mapping.verification_token = None
        mapping.verification_expires_at = None
        mapping.last_verified_at = datetime.utcnow()
        
        await db.commit()

        if mapping.platform_type == "slack":
            from app.models.external_platform import ExternalPlatform
            from app.services.platform_adapters.slack_adapter import SlackAdapter

            platform = await db.get(ExternalPlatform, mapping.platform_id)
            if not platform:
                return {"success": False, "error": "Platform not found"}

            adapter = SlackAdapter(platform)
            await adapter.send_dm(
                mapping.external_user_id,
                "✅ Your account has been verified! You can now use the Slack integration."
            )

        return {
            "success": True,
            "mapping_id": mapping.id,
            "external_user_id": mapping.external_user_id,
            "platform_type": mapping.platform_type
        }