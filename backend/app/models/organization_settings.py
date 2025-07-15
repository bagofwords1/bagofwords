from sqlalchemy import Column, String, ForeignKey, JSON
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
from app.schemas.organization_settings_schema import OrganizationSettingsConfig, FeatureConfig, FeatureState

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
        If the config represents a feature, returns a FeatureConfig object.
        Otherwise, returns the raw value.
        """
        db_value_dict = None
        if key in self.config:
            db_value_dict = self.config.get(key)
        # Check in ai_features if not found directly
        elif "ai_features" in self.config and key in self.config["ai_features"]:
            db_value_dict = self.config["ai_features"].get(key)
            
        if db_value_dict is not None:
            # If it looks like a feature config (has 'name', 'description'), return FeatureConfig
            if isinstance(db_value_dict, dict) and all(k in db_value_dict for k in ['name', 'description']):
                 # Ensure 'value' exists, deriving from 'state' if needed (matching schema logic)
                 if 'value' not in db_value_dict:
                      db_value_dict['value'] = db_value_dict.get('state') == FeatureState.ENABLED
                 return FeatureConfig(**db_value_dict)
            # Otherwise, return the raw value (could be a simple value like int or bool stored directly)
            return db_value_dict
            
        # If not in database, try to get default from schema
        try:
            config_model = OrganizationSettingsConfig()
            if hasattr(config_model, key):
                default_value = getattr(config_model, key)
                # Return FeatureConfig or raw value based on type
                return default_value if isinstance(default_value, FeatureConfig) else default_value
            elif key in config_model.ai_features:
                return config_model.ai_features[key] # This is already a FeatureConfig
        except (ImportError, AttributeError):
            pass
            
        # If all else fails, return the provided default
        return default
    
    def set_config(self, key, value):
        """Set a configuration value by key."""
        if self.config is None:
            self.config = {}
        self.config[key] = value 