from sqlalchemy import Column, String, ForeignKey, Boolean, DateTime, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
from datetime import datetime

class ExternalUserMapping(BaseSchema):
    __tablename__ = "external_user_mappings"
    
    organization_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    platform_id = Column(String(36), ForeignKey("external_platforms.id"), nullable=False)
    platform_type = Column(String, nullable=False)  # 'slack', 'teams', 'email'
    external_user_id = Column(String, nullable=False)  # Platform-specific user ID
    external_email = Column(String, nullable=True)  # For verification
    external_name = Column(String, nullable=True)  # Platform-specific user name
    app_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)  # Allow null initially
    is_verified = Column(Boolean, default=False, nullable=False)
    verification_token = Column(String, nullable=True, unique=True)
    verification_expires_at = Column(DateTime, nullable=True)
    last_verified_at = Column(DateTime, nullable=True)
    
    # Relationships
    organization = relationship("Organization", back_populates="external_user_mappings")
    user = relationship("User", back_populates="external_user_mappings")
    external_platform = relationship("ExternalPlatform", back_populates="external_user_mappings")
    
    # Composite unique constraint
    __table_args__ = (
        UniqueConstraint('organization_id', 'platform_type', 'external_user_id', name='unique_external_user'),
    )
    
    def __repr__(self):
        return f"<ExternalUserMapping {self.platform_type}:{self.external_user_id} -> {self.app_user_id}>"