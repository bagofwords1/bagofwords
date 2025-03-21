from sqlalchemy import Column, String, DateTime, Text, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
from cryptography.fernet import Fernet
from app.settings.config import settings
import json
from sqlalchemy.ext.declarative import declared_attr

class GitRepository(BaseSchema):
    __tablename__ = "git_repositories"

    provider = Column(String, nullable=False)  # e.g., 'github', 'gitlab', 'bitbucket'
    repo_url = Column(String, nullable=False)
    last_indexed_at = Column(DateTime, nullable=True)
    ssh_key = Column(Text, nullable=True)  # Encrypted SSH key
    branch = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    status = Column(String, nullable=True) # pending, indexing, completed, failed
    # Foreign Keys
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False)
    data_source_id = Column(String(36), ForeignKey('data_sources.id'), nullable=True)
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False)

    # Relationships
    user = relationship("User", back_populates="git_repositories")
    data_source = relationship("DataSource", back_populates="git_repository", uselist=False)
    organization = relationship("Organization", back_populates="git_repositories")
    
    # Use lambda for late binding to avoid circular imports
    metadata_indexing_jobs = relationship(
        lambda: MetadataIndexingJob,
        back_populates="git_repository"
    )

    def encrypt_ssh_key(self, ssh_key: dict):
        """Encrypt SSH key details before storing"""
        fernet = Fernet(settings.bow_config.encryption_key)
        self.ssh_key = fernet.encrypt(json.dumps(ssh_key).encode()).decode()

    def decrypt_ssh_key(self) -> dict:
        """Decrypt stored SSH key details"""
        if not self.ssh_key:
            return None
        fernet = Fernet(settings.bow_config.encryption_key)
        return json.loads(fernet.decrypt(self.ssh_key.encode()).decode())

# Import at the end to avoid circular imports
from app.models.metadata_indexing_job import MetadataIndexingJob