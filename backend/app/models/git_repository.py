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
    custom_host = Column(String(255), nullable=True)  # For self-hosted (e.g., github.company.com)
    
    # Instruction sync settings
    auto_publish = Column(Boolean, nullable=False, default=False)  # Auto-publish synced instructions
    default_load_mode = Column(String(20), nullable=False, default='auto')  # auto, always, intelligent, disabled
    last_indexed_commit_sha = Column(String(40), nullable=True)  # Track last indexed commit
    
    # Write-back settings (BOW → Git)
    access_token = Column(Text, nullable=True)  # Encrypted PAT for HTTPS + API
    access_token_username = Column(String(255), nullable=True)  # For Bitbucket Cloud (requires username)
    write_enabled = Column(Boolean, nullable=False, default=False)  # Enable push to Git
    
    # Foreign Keys
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False)
    data_source_id = Column(String(36), ForeignKey('data_sources.id'), nullable=True)
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False)

    # Relationships
    user = relationship("User", back_populates="git_repositories")
    data_source = relationship(
        "DataSource", 
        back_populates="git_repository", 
        uselist=False, 
        lazy="selectin",
        overlaps="organization,reports"
    )
    organization = relationship("Organization", back_populates="git_repositories")
    
    # Use lambda for late binding to avoid circular imports
    metadata_indexing_jobs = relationship(
        lambda: MetadataIndexingJob,
        back_populates="git_repository"
    )

    def encrypt_ssh_key(self, ssh_key: str):
        """Encrypt SSH key before storing"""
        fernet = Fernet(settings.bow_config.encryption_key)
        self.ssh_key = fernet.encrypt(ssh_key.encode()).decode()

    def decrypt_ssh_key(self) -> str:
        """Decrypt stored SSH key"""
        if not self.ssh_key:
            return None
        fernet = Fernet(settings.bow_config.encryption_key)
        return fernet.decrypt(self.ssh_key.encode()).decode()

    def encrypt_access_token(self, token: str):
        """Encrypt PAT/access token before storing"""
        fernet = Fernet(settings.bow_config.encryption_key)
        self.access_token = fernet.encrypt(token.encode()).decode()

    def decrypt_access_token(self) -> str:
        """Decrypt stored access token"""
        if not self.access_token:
            return None
        fernet = Fernet(settings.bow_config.encryption_key)
        return fernet.decrypt(self.access_token.encode()).decode()

    # ==================== Capability Properties ====================
    
    @property
    def has_ssh_key(self) -> bool:
        """Check if SSH key is configured"""
        return bool(self.ssh_key)

    @property
    def has_access_token(self) -> bool:
        """Check if PAT/access token is configured"""
        return bool(self.access_token)

    @property
    def can_push(self) -> bool:
        """Can push to Git (requires write_enabled + either SSH or PAT)"""
        return self.write_enabled and (self.has_ssh_key or self.has_access_token)

    @property
    def can_create_pr(self) -> bool:
        """Can create PRs via API (requires write_enabled + PAT)"""
        return self.write_enabled and self.has_access_token

    @property
    def is_self_hosted(self) -> bool:
        """Check if this is a self-hosted Git provider"""
        return bool(self.custom_host)


# Import at the end to avoid circular imports
from app.models.metadata_indexing_job import MetadataIndexingJob