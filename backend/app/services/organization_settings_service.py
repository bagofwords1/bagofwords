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
    FeatureConfig,
    FeatureState
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
        else:
            # Check for any new features in schema that aren't in the DB
            await self._sync_new_features(db, settings)
            
        return settings

    async def _sync_new_features(self, db: AsyncSession, settings: OrganizationSettings):
        """Sync any new features from schema that don't exist in DB config."""
        schema_config = OrganizationSettingsConfig()
        current_config = dict(settings.config)
        config_modified = False

        # Check top-level features
        for key, feature in schema_config.dict().items():
            if key != 'ai_features' and key not in current_config:
                current_config[key] = feature
                config_modified = True

        # Check AI features
        if 'ai_features' not in current_config:
            current_config['ai_features'] = {}

        for key, feature in schema_config.ai_features.items():
            if key not in current_config['ai_features']:
                current_config['ai_features'][key] = feature.dict()
                config_modified = True

        # Only update DB if new features were added
        if config_modified:
            settings.config = current_config
            settings.updated_at = datetime.utcnow()
            flag_modified(settings, "config")
            db.add(settings)
            await db.commit()
            await db.refresh(settings)

    async def update_settings(
        self, 
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        settings_data: OrganizationSettingsUpdate
    ):
        """Update organization settings"""
        settings = await self.get_settings(db, organization, current_user)
        update_data = settings_data.dict(exclude_unset=True)
        
        if 'config' in update_data and update_data['config']:
            current_config = dict(settings.config)
            
            # Handle AI features updates
            if 'ai_features' in update_data['config']:
                ai_features_updates = update_data['config']['ai_features']
                
                if 'ai_features' not in current_config:
                    current_config['ai_features'] = {}
                    
                for feature_name, feature_data in ai_features_updates.items():
                    # Get feature from current config or schema default
                    feature_dict = current_config['ai_features'].get(
                        feature_name, 
                        OrganizationSettingsConfig().ai_features[feature_name].dict()
                    )
                    feature = FeatureConfig(**feature_dict)
                    
                    if not feature.editable or feature.state == FeatureState.LOCKED:
                        raise HTTPException(
                            status_code=403, 
                            detail=f"Feature '{feature_name}' cannot be modified"
                        )
                    
                    # Update the feature
                    if 'state' in feature_data:
                        feature.state = feature_data['state']
                    for field, value in feature_data.items():
                        if field != 'state' and hasattr(feature, field):
                            setattr(feature, field, value)
                    
                    current_config['ai_features'][feature_name] = feature.dict()
            
            # Handle top-level feature updates
            for key, value in update_data['config'].items():
                if key != 'ai_features':
                    # Get feature from current config or schema default
                    feature_dict = current_config.get(
                        key, 
                        getattr(OrganizationSettingsConfig(), key, None)
                    )
                    if feature_dict:
                        feature = FeatureConfig(**feature_dict)
                        
                        if not feature.editable or feature.state == FeatureState.LOCKED:
                            raise HTTPException(
                                status_code=403, 
                                detail=f"Feature '{key}' cannot be modified"
                            )
                        
                        if isinstance(value, dict):
                            if 'state' in value:
                                feature.state = value['state']
                            for field, field_value in value.items():
                                if field != 'state' and hasattr(feature, field):
                                    setattr(feature, field, field_value)
                            current_config[key] = feature.dict()
                        else:
                            current_config[key] = value
            
            settings.config = current_config
            settings.updated_at = datetime.utcnow()
            flag_modified(settings, "config")
            
            await db.commit()
            await db.refresh(settings)
        
        return settings

    async def create_default_settings(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User
    ):
        """Create default settings for a new organization"""
        config = OrganizationSettingsConfig()
        settings = OrganizationSettings(
            organization_id=organization.id,
            config=config.dict()
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
        
        # Get the feature configuration
        feature = settings.get_config(feature_name)
        if not feature:
            raise HTTPException(status_code=404, detail=f"AI feature '{feature_name}' not found")
            
        try:
            if enabled:
                feature.enable()
            else:
                feature.disable()
        except ValueError as e:
            raise HTTPException(status_code=403, detail=str(e))
        
        # Update the config in the database
        if "ai_features" not in settings.config:
            settings.config["ai_features"] = {}
        settings.config["ai_features"][feature_name] = feature.dict()
        
        flag_modified(settings, "config")
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
        
        return settings
