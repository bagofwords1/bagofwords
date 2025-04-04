from sqlalchemy import Column, String, ForeignKey, JSON
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
from app.schemas.organization_settings_schema import OrganizationSettingsConfig, FeatureConfig

class OrganizationSettings(BaseSchema):
    __tablename__ = "organization_settings"
    
    organization_id = Column(String, ForeignKey("organizations.id"), unique=True, nullable=False)
    config = Column(JSON, default=dict, nullable=False)
    
    # Relationship back to the organization
    organization = relationship("Organization", back_populates="settings")
    
    def get_config(self, key, default=None):
        """Get a configuration value by key.
        
        First checks the database config, if not found,
        falls back to OrganizationSettingsConfig defaults,
        and finally uses the provided default value.
        """
        # First check if value exists in database config
        db_value = None
        if key in self.config:
            db_value = self.config.get(key)
        # Check in ai_features if not found directly
        elif "ai_features" in self.config and key in self.config["ai_features"]:
            db_value = self.config["ai_features"].get(key)
            
        if db_value is not None:
            # Convert dictionary to FeatureConfig if it's from ai_features
            if isinstance(db_value, dict) and "enabled" in db_value:
                return FeatureConfig(**db_value)
            return db_value
            
        # If not in database, try to get default from schema
        try:
            config_model = OrganizationSettingsConfig()
            if hasattr(config_model, key):
                return getattr(config_model, key)
            elif key in config_model.ai_features:
                return config_model.ai_features[key]
        except (ImportError, AttributeError):
            pass
            
        # If all else fails, return the provided default
        return default
    
    def set_config(self, key, value):
        """Set a configuration value by key."""
        if self.config is None:
            self.config = {}
        self.config[key] = value 