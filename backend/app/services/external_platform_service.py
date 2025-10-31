from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from fastapi import HTTPException
from typing import List, Optional
import json

from app.models.external_platform import ExternalPlatform
from app.models.organization import Organization
from app.models.user import User
from app.schemas.external_platform_schema import (
    ExternalPlatformCreate, 
    ExternalPlatformUpdate, 
    ExternalPlatformSchema,
    SlackConfig,
    TeamsConfig,
    EmailConfig
)
from app.core.telemetry import telemetry

class ExternalPlatformService:
    
    async def create_platform(
        self, 
        db: AsyncSession, 
        organization: Organization,
        platform_data: ExternalPlatformCreate,
        current_user: User
    ) -> ExternalPlatformSchema:
        """Create a new external platform for an organization"""
        
        # Check if platform type already exists for this organization
        existing_platform = await self.get_platform_by_type(
            db, organization.id, platform_data.platform_type
        )
        if existing_platform:
            raise HTTPException(
                status_code=400, 
                detail=f"{platform_data.platform_type} platform already exists for this organization"
            )
        
        # Create platform
        platform = ExternalPlatform(
            organization_id=organization.id,
            platform_type=platform_data.platform_type,
            platform_config=platform_data.platform_config,
            is_active=platform_data.is_active
        )
        
        db.add(platform)
        await db.commit()
        await db.refresh(platform)
        # Telemetry: external platform created
        try:
            await telemetry.capture(
                "external_platform_created",
                {
                    "platform_id": str(platform.id),
                    "platform_type": platform.platform_type,
                    "is_active": bool(platform.is_active),
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass
        
        return ExternalPlatformSchema.from_orm(platform)
    
    async def get_platforms(
        self, 
        db: AsyncSession, 
        organization: Organization
    ) -> List[ExternalPlatformSchema]:
        """Get all external platforms for an organization"""
        
        stmt = select(ExternalPlatform).where(
            ExternalPlatform.organization_id == organization.id
        )
        result = await db.execute(stmt)
        platforms = result.scalars().all()
        
        return [ExternalPlatformSchema.from_orm(platform) for platform in platforms]
    
    async def get_platform_by_id(
        self, 
        db: AsyncSession, 
        platform_id: str,
        organization: Organization
    ) -> ExternalPlatform:
        """Get a specific platform by ID"""
        
        stmt = select(ExternalPlatform).where(
            and_(
                ExternalPlatform.id == platform_id,
                ExternalPlatform.organization_id == organization.id
            )
        )
        result = await db.execute(stmt)
        platform = result.scalar_one_or_none()
        
        if not platform:
            raise HTTPException(status_code=404, detail="External platform not found")
        
        return platform
    
    async def get_platform_by_type(
        self, 
        db: AsyncSession, 
        organization_id: str,
        platform_type: str
    ) -> Optional[ExternalPlatform]:
        """Get platform by type for an organization"""
        
        stmt = select(ExternalPlatform).where(
            and_(
                ExternalPlatform.organization_id == organization_id,
                ExternalPlatform.platform_type == platform_type
            )
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()
    
    async def update_platform(
        self, 
        db: AsyncSession, 
        platform_id: str,
        platform_data: ExternalPlatformUpdate,
        organization: Organization
    ) -> ExternalPlatformSchema:
        """Update an external platform"""
        
        platform = await self.get_platform_by_id(db, platform_id, organization)
        
        # Update fields
        if platform_data.platform_config is not None:
            platform.platform_config = platform_data.platform_config
        if platform_data.is_active is not None:
            platform.is_active = platform_data.is_active
        
        await db.commit()
        await db.refresh(platform)
        
        return ExternalPlatformSchema.from_orm(platform)
    
    async def delete_platform(
        self, 
        db: AsyncSession, 
        platform_type: str,
        organization: Organization
    ) -> bool:
        """Delete an external platform"""
        
        platform = await self.get_platform_by_type(db, organization.id, platform_type)
        
        await db.delete(platform)
        await db.commit()
        # Telemetry: external platform deleted
        try:
            await telemetry.capture(
                "external_platform_deleted",
                {
                    "platform_type": platform_type,
                },
                user_id=None,
                org_id=organization.id,
            )
        except Exception:
            pass
        
        return True
    
    async def test_platform_connection(
        self, 
        db: AsyncSession, 
        platform_id: str,
        organization: Organization
    ) -> dict:
        """Test the connection to an external platform"""
        
        platform = await self.get_platform_by_id(db, platform_id, organization)
        
        try:
            if platform.platform_type == "slack":
                return await self._test_slack_connection(platform)
            elif platform.platform_type == "teams":
                return await self._test_teams_connection(platform)
            elif platform.platform_type == "email":
                return await self._test_email_connection(platform)
            else:
                raise HTTPException(status_code=400, detail="Unsupported platform type")
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def _test_slack_connection(self, platform: ExternalPlatform) -> dict:
        """Test Slack connection"""
        try:
            import httpx
            
            # Extract bot token from config
            config = platform.platform_config
            bot_token = config.get("bot_token")
            
            if not bot_token:
                return {"success": False, "error": "Bot token not configured"}
            
            # Test API call
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {bot_token}"}
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok"):
                        return {
                            "success": True, 
                            "workspace": data.get("team"),
                            "bot_user": data.get("user")
                        }
                    else:
                        return {"success": False, "error": data.get("error", "Unknown error")}
                else:
                    return {"success": False, "error": f"HTTP {response.status_code}"}
                    
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def _test_teams_connection(self, platform: ExternalPlatform) -> dict:
        """Test Teams connection (placeholder)"""
        return {"success": False, "error": "Teams integration not yet implemented"}
    
    async def _test_email_connection(self, platform: ExternalPlatform) -> dict:
        """Test Email connection (placeholder)"""
        return {"success": False, "error": "Email integration not yet implemented"}

    async def create_slack_platform(
        self, 
        db: AsyncSession, 
        organization: Organization,
        bot_token: str,
        signing_secret: str,
        current_user: User
    ) -> ExternalPlatformSchema:
        """Create a Slack platform with proper configuration"""
        # Test the bot token first
        test_result = await self._test_slack_token(bot_token)
        if not test_result.get("success"):
            raise HTTPException(
                status_code=400, 
                detail=f"Invalid bot token: {test_result.get('error')}"
            )
        
        # Extract team info from test result
        team_info = test_result.get("workspace", {})
        team_id = team_info.get("id")
        team_name = team_info.get("name")
        
        # Check if platform already exists for this team
        existing_platform = await self.get_platform_by_type(db, organization.id, "slack")
        if existing_platform:
            raise HTTPException(
                status_code=400, 
                detail="Slack integration already exists for this organization"
            )
        
        # Create platform config
        platform_config = {
            "team_id": team_id,
            "team_name": team_name,
            "base_url": "https://your-domain.com"  # Update this
        }
        
        # Create credentials
        credentials = {
            "bot_token": bot_token,
            "signing_secret": signing_secret
        }
        
        # Create platform
        platform = ExternalPlatform(
            organization_id=organization.id,
            platform_type="slack",
            platform_config=platform_config,
            is_active=True
        )
        
        # Encrypt and store credentials
        platform.encrypt_credentials(credentials)
        
        db.add(platform)
        await db.commit()
        await db.refresh(platform)
        # Telemetry: external platform created (slack)
        try:
            await telemetry.capture(
                "external_platform_created",
                {
                    "platform_id": str(platform.id),
                    "platform_type": "slack",
                    "is_active": True,
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass
        
        return ExternalPlatformSchema.from_orm(platform)

    async def _test_slack_token(self, bot_token: str) -> dict:
        """Test Slack bot token and get workspace info"""
        
        try:
            import httpx
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {bot_token}"}
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok"):
                        return {
                            "success": True,
                            "workspace": {
                                "id": data.get("team_id"),
                                "name": data.get("team")
                            },
                            "bot_user": data.get("user")
                        }
                    else:
                        return {"success": False, "error": data.get("error", "Unknown error")}
                else:
                    return {"success": False, "error": f"HTTP {response.status_code}"}
                    
        except Exception as e:
            return {"success": False, "error": str(e)}