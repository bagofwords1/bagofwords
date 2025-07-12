from sqlalchemy.ext.asyncio import AsyncSession
from typing import Dict, Any, Optional
from app.models.external_platform import ExternalPlatform
from app.models.external_user_mapping import ExternalUserMapping
from app.schemas.external_user_mapping_schema import ExternalUserMappingCreate
from app.services.platform_adapters.adapter_factory import PlatformAdapterFactory
from app.services.platform_adapters.base_adapter import PlatformAdapter  # Add this import
from app.services.external_platform_service import ExternalPlatformService
from app.services.external_user_mapping_service import ExternalUserMappingService
from app.services.organization_service import OrganizationService
class ExternalPlatformManager:
    """Manages external platform interactions"""
    
    def __init__(self):
        self.platform_service = ExternalPlatformService()
        self.mapping_service = ExternalUserMappingService()
        self.organization_service = OrganizationService()
    
    async def handle_incoming_message(
        self, 
        db: AsyncSession, 
        platform_type: str,
        organization_id: str,
        event_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Handle incoming message from external platform"""
        
        try:
            # Get platform
            platform = await self.platform_service.get_platform_by_type(
                db, organization_id, platform_type
            )
            if not platform or not platform.is_active:
                return {"success": False, "error": "Platform not found or inactive"}
            
            # Create adapter
            adapter = PlatformAdapterFactory.create_adapter(platform)
            
            # Process message
            processed_data = await adapter.process_incoming_message(event_data)
            

            # Get or create user mapping
            user_mapping = await self._get_or_create_user_mapping(
                db, platform, processed_data, adapter
            )

            if not user_mapping:
                return {"success": False, "error": "User mapping not found"}
            
            if not user_mapping.is_verified:
                # Send verification message
                await self._handle_unverified_user(
                    db, adapter, processed_data, user_mapping
                )
                return {"success": True, "action": "verification_sent"}
            
            # Process verified message
            return await self._process_verified_message(
                db, adapter, processed_data, user_mapping
            )
            
        except Exception as e:
            print(f"Error handling incoming message: {e}")
            return {"success": False, "error": str(e)}
    
    async def _get_or_create_user_mapping(
        self, 
        db: AsyncSession, 
        platform: ExternalPlatform,
        processed_data: Dict[str, Any],
        adapter: PlatformAdapter
    ) -> Optional[ExternalUserMapping]:
        """Get or create user mapping"""
        
        external_user_id = processed_data.get("external_user_id")
        if not external_user_id:
            return None
        
        # Try to find existing mapping
        mapping = await self.mapping_service.get_mapping_by_external_id(
            db, platform.organization_id, platform.platform_type, external_user_id
        )
        
        if mapping:
            return mapping
        
        # Create unverified mapping (no app_user_id yet)
        mapping_data = ExternalUserMappingCreate(
            platform_type=platform.platform_type,
            external_user_id=external_user_id,
            external_email=None,  # Will be filled after verification
            external_name=None,   # Will be filled after verification
            app_user_id=None,     # Will be filled after verification
            is_verified=False
        )
        
        # Get organization for the mapping service
        organization = await self.organization_service.get_organization(db, platform.organization_id, None)
        
        try:
            # Pass the platform ID to the create_mapping method
            mapping = await self.mapping_service.create_mapping(db, organization, mapping_data, platform.id)
            return mapping
        except Exception as e:
            print(f"Error creating mapping: {e}")
            return None
    
    async def _handle_unverified_user(
        self, 
        db: AsyncSession, 
        adapter: PlatformAdapter,
        processed_data: Dict[str, Any],
        user_mapping: ExternalUserMapping
    ):
        """Handle unverified user - send verification link"""
        # Get organization for the mapping service
        organization = await self.organization_service.get_organization(db, user_mapping.organization_id, None)
        
        # Generate verification token
        token = await self.mapping_service.generate_verification_token(
            db, user_mapping.id, organization
        )

        # Send verification message with link
        await adapter.send_verification_message(
            processed_data.get("channel_id"),
            None,  # No email needed
            token
        )

    async def _process_verified_message(
        self, 
        db: AsyncSession, 
        adapter: PlatformAdapter,
        processed_data: Dict[str, Any],
        user_mapping: ExternalUserMapping
    ) -> Dict[str, Any]:
        """Process message from verified user"""
        
        # This will integrate with your existing completion system
        # For now, just return success

        # Send hello message back through the adapter
        await adapter.send_dm(
            user_mapping.external_user_id,
            "Hello there!"
        )

        return {
            "success": True,
            "action": "message_processed",
            "user_id": user_mapping.app_user_id,
            "message": processed_data.get("message_text")
        }
