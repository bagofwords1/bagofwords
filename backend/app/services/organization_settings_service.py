from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from sqlalchemy.orm.attributes import flag_modified

from app.models.organization import Organization
from app.models.user import User
from app.models.organization_settings import OrganizationSettings
from app.schemas.organization_settings_schema import (
    OrganizationSettingsCreate, 
    OrganizationSettingsUpdate,
    OrganizationSettingsConfig,
    FeatureConfig
)
from datetime import datetime


class OrganizationSettingsService:
    def __init__(self):
        pass

    async def get_settings(
        self, 
        db: AsyncSession, 
        organization: Organization,
        current_user: User
    ):
        """Get settings for an organization"""
        result = await db.execute(
            select(OrganizationSettings)
            .filter(OrganizationSettings.organization_id == organization.id)
        )
        
        settings = result.scalar_one_or_none()
        
        # If settings don't exist yet, create default ones
        if not settings:
            settings = await self.create_default_settings(db, organization, current_user)
            
        return settings

    async def update_settings(
        self, 
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        settings_data: OrganizationSettingsUpdate
    ):
        """Update organization settings"""
        # Fetch current settings
        result = await db.execute(
            select(OrganizationSettings)
            .filter(OrganizationSettings.organization_id == organization.id)
        )
        
        settings = result.scalar_one_or_none()
        
        # If settings don't exist yet, create default ones
        if not settings:
            settings = await self.create_default_settings(db, organization, current_user)
        
        # Process updates
        update_data = settings_data.dict(exclude_unset=True)
        
        if 'config' in update_data and update_data['config']:
            # Print state before update
            print("SETTINGS BEFORE UPDATE:", settings.config)
            print("UPDATE DATA:", update_data['config'])
            
            # Create a copy of the current config
            current_config = dict(settings.config)
            
            # Apply updates to the copy
            if 'ai_features' in update_data['config']:
                ai_features_updates = update_data['config']['ai_features']
                
                # Ensure ai_features exists
                if 'ai_features' not in current_config:
                    current_config['ai_features'] = {}
                    
                # Apply each feature update directly
                for feature_name, feature_data in ai_features_updates.items():
                    if feature_name in current_config['ai_features']:
                        feature = current_config['ai_features'][feature_name]
                        
                        # Check if editable
                        if feature.get('editable', True):
                            # Apply all fields in the update
                            for field, value in feature_data.items():
                                print(f"Updating ai_features.{feature_name}.{field} from {feature.get(field)} to {value}")
                                feature[field] = value
            
            # Update other top-level config fields (like allow_llm_see_data)
            for key, value in update_data['config'].items():
                if key != 'ai_features' and key in current_config:
                    # If this is a partial update (e.g., just the 'enabled' field)
                    if isinstance(value, dict) and isinstance(current_config[key], dict):
                        # Only update the provided fields, preserving other fields
                        for field_name, field_value in value.items():
                            print(f"Updating {key}.{field_name} from {current_config[key].get(field_name)} to {field_value}")
                            current_config[key][field_name] = field_value
                    else:
                        # Complete replacement of the field
                        current_config[key] = value
            
            # Replace the entire config dictionary to force SQLAlchemy to detect the change
            settings.config = current_config
        
        # Update timestamp
        settings.updated_at = datetime.utcnow()
        
        # Print state after changes but before commit
        print("SETTINGS AFTER UPDATE:", settings.config)
        
        # Force SQLAlchemy to detect the config change
        flag_modified(settings, "config")
        
        # Commit changes
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
        
        # Print state after commit
        print("SETTINGS AFTER COMMIT:", settings.config)
        
        return settings

    async def create_default_settings(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User
    ):
        """Create default settings for a new organization"""
        # Create settings with a serializable config
        settings = OrganizationSettings(
            organization_id=organization.id,
            config=OrganizationSettingsConfig().dict()  # Convert to dictionary before saving
        )
        
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
        
        return settings

    async def update_ai_feature(
        self, 
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        feature_name: str,
        enabled: bool
    ):
        """Update a specific AI feature setting"""
        settings = await self.get_settings(db, organization, current_user)
        
        if feature_name in settings.config.ai_features:
            feature = settings.config.ai_features[feature_name]
            
            # Only update if the feature is editable
            if feature.editable:
                feature.enabled = enabled
            else:
                raise HTTPException(status_code=403, detail=f"AI feature '{feature_name}' is not editable")
        else:
            raise HTTPException(status_code=404, detail=f"AI feature '{feature_name}' not found")
        
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
        
        return settings
