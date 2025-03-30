from sqlalchemy import Column, String, ForeignKey, JSON
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema

class OrganizationSettings(BaseSchema):
    __tablename__ = "organization_settings"
    
    organization_id = Column(String, ForeignKey("organizations.id"), unique=True, nullable=False)
    config = Column(JSON, default=dict, nullable=False)
    
    # Relationship back to the organization
    organization = relationship("Organization", back_populates="settings")
    
    def get_config(self, key, default=None):
        """Get a configuration value by key."""
        return self.config.get(key, default)
    
    def set_config(self, key, value):
        """Set a configuration value by key."""
        if self.config is None:
            self.config = {}
        self.config[key] = value 