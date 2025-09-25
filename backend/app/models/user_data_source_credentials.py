from sqlalchemy import Column, String, Text, Boolean, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema
from cryptography.fernet import Fernet
from app.settings.config import settings
import json


class UserDataSourceCredentials(BaseSchema):
    __tablename__ = "user_data_source_credentials"

    data_source_id = Column(String(36), ForeignKey("data_sources.id"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)

    # Registry auth mode key (e.g., 'userpass', 'iam', 'arn', 'pat')
    auth_mode = Column(String(64), nullable=False)

    # Encrypted JSON blob of credentials
    encrypted_credentials = Column(Text, nullable=False)

    # Lifecycle / management
    is_active = Column(Boolean, nullable=False, default=True)
    is_primary = Column(Boolean, nullable=False, default=True)
    last_used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    # Optional non-secret metadata for diagnostics (last error, client info, etc.)
    metadata_json = Column(JSON, nullable=True)

    # Relationships
    data_source = relationship("DataSource", backref="user_credentials", lazy="selectin")
    # Match user.py: user.user_data_source_credentials back_populates
    user = relationship("User", back_populates="user_data_source_credentials", lazy="selectin")
    organization = relationship("Organization", backref="user_data_source_credentials", lazy="selectin")

    def encrypt_credentials(self, payload: dict) -> None:
        fernet = Fernet(settings.bow_config.encryption_key)
        self.encrypted_credentials = fernet.encrypt(json.dumps(payload).encode()).decode()

    def decrypt_credentials(self) -> dict:
        if not self.encrypted_credentials:
            return {}
        fernet = Fernet(settings.bow_config.encryption_key)
        return json.loads(fernet.decrypt(self.encrypted_credentials.encode()).decode())


