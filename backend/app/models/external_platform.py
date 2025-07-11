from sqlalchemy import Column, String, ForeignKey, Boolean, JSON, Text
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
from cryptography.fernet import Fernet
from app.settings.config import settings
import json

class ExternalPlatform(BaseSchema):
    __tablename__ = "external_platforms"
    
    organization_id = Column(String, ForeignKey("organizations.id"), nullable=False)
    platform_type = Column(String, nullable=False)  # 'slack', 'teams', 'email'
    platform_config = Column(JSON, nullable=False)  # Platform-specific configuration
    credentials = Column(Text, nullable=True)  # Encrypted sensitive credentials
    is_active = Column(Boolean, default=True, nullable=False)
    
    # Relationships
    organization = relationship("Organization", back_populates="external_platforms")
    external_user_mappings = relationship("ExternalUserMapping", back_populates="external_platform")
    
    def encrypt_credentials(self, credentials: dict):
        """Encrypt sensitive credentials before storing"""
        fernet = Fernet(settings.bow_config.encryption_key)
        self.credentials = fernet.encrypt(json.dumps(credentials).encode()).decode()
    
    def decrypt_credentials(self) -> dict:
        """Decrypt stored credentials"""
        if not self.credentials:
            return {}
        fernet = Fernet(settings.bow_config.encryption_key)
        return json.loads(fernet.decrypt(self.credentials.encode()).decode())
    
    def __repr__(self):
        return f"<ExternalPlatform {self.platform_type}:{self.id} - {self.organization.name}>"