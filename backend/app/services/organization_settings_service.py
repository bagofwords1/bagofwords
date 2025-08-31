from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException, UploadFile
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
import os
import hashlib
from PIL import Image
from io import BytesIO


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
        # Ensure current_config is mutable and handles potential None
        current_config = dict(settings.config) if settings.config else {}
        config_modified = False

        # Ensure top-level keys from schema exist
        schema_dict = schema_config.dict(exclude={'ai_features'})
        for key, feature_or_value in schema_dict.items():
             if key not in current_config:
                 # Store the dict representation if it's a FeatureConfig
                 current_config[key] = feature_or_value if not isinstance(feature_or_value, FeatureConfig) else feature_or_value.dict()
                 config_modified = True

        # Ensure 'ai_features' key exists and sync individual AI features
        if 'ai_features' not in current_config:
            current_config['ai_features'] = {}
            config_modified = True # Mark modified if ai_features dict itself was added

        schema_ai_features = schema_config.ai_features
        # Ensure current_config['ai_features'] is a dict
        if not isinstance(current_config.get('ai_features'), dict):
            current_config['ai_features'] = {}
            config_modified = True

        for key, feature in schema_ai_features.items():
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
        # Ensure settings.config is a dictionary
        if settings.config is None:
             settings.config = {}
             flag_modified(settings, "config") # Mark as modified if initialized

        update_data = settings_data.dict(exclude_unset=True)

        if 'config' in update_data and update_data['config']:
            # Use dict() to ensure we have a mutable copy
            current_config = dict(settings.config)
            config_changed = False

            # Handle AI features updates
            if 'ai_features' in update_data['config']:
                ai_features_updates = update_data['config']['ai_features']

                if 'ai_features' not in current_config:
                    current_config['ai_features'] = {}

                for feature_name, feature_data in ai_features_updates.items():
                    # Get current feature config from DB or default from schema
                    current_feature_dict = current_config['ai_features'].get(feature_name)
                    if not current_feature_dict:
                         # Feature not in DB, get default from schema
                         default_feature = OrganizationSettingsConfig().ai_features.get(feature_name)
                         if not default_feature: continue # Skip if feature unknown
                         current_feature_dict = default_feature.dict()
                         current_config['ai_features'][feature_name] = current_feature_dict # Add to config

                    # Create FeatureConfig object from current data to check properties
                    feature = FeatureConfig(**current_feature_dict)

                    if not feature.editable or feature.state == FeatureState.LOCKED:
                         # Allow updating non-editable/locked features only if the update doesn't change 'value' or 'state'
                         can_update = True
                         if 'value' in feature_data and feature_data['value'] != feature.value:
                             can_update = False
                         if 'state' in feature_data and feature_data['state'] != feature.state:
                             can_update = False

                         if not can_update:
                              raise HTTPException(
                                  status_code=403,
                                  detail=f"Feature '{feature_name}' cannot be modified"
                              )

                    # Apply updates from feature_data to the dictionary
                    original_dict = current_feature_dict.copy()
                    for field, value in feature_data.items():
                         if hasattr(feature, field): # Check if field is valid for FeatureConfig
                             current_feature_dict[field] = value

                    # Re-validate and potentially adjust state/value based on changes
                    updated_feature = FeatureConfig(**current_feature_dict)
                    current_config['ai_features'][feature_name] = updated_feature.dict()

                    if current_config['ai_features'][feature_name] != original_dict:
                        config_changed = True


            # Handle top-level feature updates
            for key, value_update in update_data['config'].items():
                if key != 'ai_features':
                    # Get current config dict from DB or default from schema
                    current_value_dict = current_config.get(key)
                    is_feature = False
                    default_config = getattr(OrganizationSettingsConfig(), key, None)

                    if isinstance(default_config, FeatureConfig):
                         is_feature = True
                         if not current_value_dict:
                             # Feature not in DB, get default from schema
                             current_value_dict = default_config.dict()
                             current_config[key] = current_value_dict # Add to config


                    if is_feature and isinstance(current_value_dict, dict):
                         feature = FeatureConfig(**current_value_dict)

                         if not feature.editable or feature.state == FeatureState.LOCKED:
                            can_update = True
                            if isinstance(value_update, dict):
                                if 'value' in value_update and value_update['value'] != feature.value:
                                    can_update = False
                                if 'state' in value_update and value_update['state'] != feature.state:
                                    can_update = False
                            # Allow updating if it's not a dict (e.g. direct value update) only if value doesn't change
                            elif value_update != feature.value:
                                 can_update = False


                            if not can_update:
                                raise HTTPException(
                                     status_code=403,
                                     detail=f"Feature '{key}' cannot be modified"
                                )

                         original_dict = current_value_dict.copy()
                         if isinstance(value_update, dict):
                             for field, field_value in value_update.items():
                                 if hasattr(feature, field):
                                     current_value_dict[field] = field_value
                         else:
                             # Assume direct update is for the 'value' field
                             current_value_dict['value'] = value_update

                         # Re-validate and potentially adjust state/value
                         updated_feature = FeatureConfig(**current_value_dict)
                         current_config[key] = updated_feature.dict()

                         if current_config[key] != original_dict:
                             config_changed = True
                    elif key in current_config and current_config[key] != value_update : # Handle non-feature config update or addition
                         current_config[key] = value_update
                         config_changed = True
                    elif key not in current_config: # Handle adding new non-feature key
                         current_config[key] = value_update
                         config_changed = True


            if config_changed:
                settings.config = current_config
                settings.updated_at = datetime.utcnow()
                flag_modified(settings, "config")

                db.add(settings) # Add settings to session if changed
                await db.commit()
                await db.refresh(settings)

        return settings

    async def set_general_icon(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        file: UploadFile
    ):
        """Validate, process (square resize), store icon on disk and update settings.general.icon fields."""
        settings = await self.get_settings(db, organization, current_user)
        if settings.config is None:
            settings.config = {}

        content_type = (file.content_type or "").lower()
        if content_type not in ("image/png", "image/jpeg", "image/jpg"):
            raise HTTPException(status_code=400, detail="Unsupported image type. Use PNG or JPEG")

        raw = await file.read()
        if len(raw) > 512 * 1024:
            raise HTTPException(status_code=400, detail="Icon too large. Max 512KB")

        try:
            image = Image.open(BytesIO(raw))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid image file")

        # Convert to RGBA for consistent output
        image = image.convert("RGBA")
        width, height = image.size
        # center-crop to square
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        image = image.crop((left, top, left + side, top + side))
        # resize to 256x256
        image = image.resize((256, 256))

        # storage path
        base_dir = os.path.abspath(os.path.join(os.getcwd(), "branding_uploads"))
        os.makedirs(base_dir, exist_ok=True)

        digest = hashlib.sha256(raw).hexdigest()[:16]
        filename = f"{organization.id}-{digest}.png"
        file_path = os.path.join(base_dir, filename)

        # save as PNG
        with open(file_path, "wb") as f:
            buf = BytesIO()
            image.save(buf, format="PNG")
            f.write(buf.getvalue())

        # update settings
        general = dict(settings.config.get("general", {}))
        general["icon_key"] = filename
        general["icon_url"] = f"/api/general/icon/{filename}"
        settings.config["general"] = general

        flag_modified(settings, "config")
        db.add(settings)
        await db.commit()
        await db.refresh(settings)

        return settings

    async def remove_general_icon(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User
    ):
        settings = await self.get_settings(db, organization, current_user)
        if settings.config is None:
            settings.config = {}

        general = dict(settings.config.get("general", {}))
        icon_key = general.get("icon_key")
        if icon_key:
            base_dir = os.path.abspath(os.path.join(os.getcwd(), "branding_uploads"))
            file_path = os.path.join(base_dir, icon_key)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass
        general["icon_key"] = None
        general["icon_url"] = None
        settings.config["general"] = general

        flag_modified(settings, "config")
        db.add(settings)
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
        # Use the .dict() method which now correctly handles value/state consistency
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
        # Changed parameter name from 'enabled' to 'value'
        new_value: bool # Assuming this endpoint is for boolean toggles
    ):
        """Update a specific AI feature setting's value"""
        settings = await self.get_settings(db, organization, current_user)
        if settings.config is None: settings.config = {} # Ensure config exists

        # Get the feature configuration using the model's method
        feature = settings.get_config(feature_name)

        if not isinstance(feature, FeatureConfig):
             # Might be a non-feature config, or doesn't exist
             schema_config = OrganizationSettingsConfig()
             if feature_name in schema_config.ai_features:
                 # Feature exists in schema but not DB, use default
                 feature = schema_config.ai_features[feature_name]
             else:
                 raise HTTPException(status_code=404, detail=f"Feature '{feature_name}' not found or is not a valid feature configuration.")


        if not feature.editable or feature.state == FeatureState.LOCKED:
            raise HTTPException(
                status_code=403,
                detail=f"Feature '{feature_name}' cannot be modified."
            )

        # Update the feature's value
        feature.value = new_value
        # State will be updated automatically by FeatureConfig validator/dict if it's not LOCKED

        # Update the config in the database
        if "ai_features" not in settings.config or not isinstance(settings.config.get("ai_features"), dict):
            settings.config["ai_features"] = {}

        # Store the updated feature as a dict
        settings.config["ai_features"][feature_name] = feature.dict()

        flag_modified(settings, "config")
        db.add(settings)
        await db.commit()
        await db.refresh(settings)

        return settings
