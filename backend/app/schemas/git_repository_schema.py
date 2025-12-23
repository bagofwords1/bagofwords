from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import json
from app.schemas.base import OptionalUTCDatetime, UTCDatetime


class GitRepositoryBase(BaseModel):
    provider: str  # e.g., 'github', 'gitlab', 'bitbucket'
    repo_url: str
    branch: str = "main"
    is_active: bool = True
    auto_publish: bool = False  # Auto-publish synced instructions
    default_load_mode: str = "auto"  # auto, always, intelligent, disabled
    custom_host: Optional[str] = None  # For self-hosted (e.g., github.company.com)
    write_enabled: bool = False  # Enable push to Git


class GitRepositorySchema(GitRepositoryBase):
    id: str
    user_id: str
    organization_id: str
    data_source_id: Optional[str]
    last_indexed_at: OptionalUTCDatetime
    created_at: UTCDatetime
    updated_at: UTCDatetime
    status: Optional[str] = None
    
    # Capability indicators (computed from model, don't expose secrets)
    has_ssh_key: bool = False
    has_access_token: bool = False
    can_push: bool = False
    can_create_pr: bool = False
    
    class Config:
        from_attributes = True
    
    @classmethod
    def from_orm_with_capabilities(cls, obj):
        """Create schema from ORM object with capability properties"""
        data = {
            "id": obj.id,
            "provider": obj.provider,
            "repo_url": obj.repo_url,
            "branch": obj.branch,
            "is_active": obj.is_active,
            "auto_publish": obj.auto_publish,
            "default_load_mode": obj.default_load_mode,
            "custom_host": obj.custom_host,
            "write_enabled": obj.write_enabled,
            "user_id": obj.user_id,
            "organization_id": obj.organization_id,
            "data_source_id": obj.data_source_id,
            "last_indexed_at": obj.last_indexed_at,
            "created_at": obj.created_at,
            "updated_at": obj.updated_at,
            "status": obj.status,
            # Capability indicators
            "has_ssh_key": obj.has_ssh_key,
            "has_access_token": obj.has_access_token,
            "can_push": obj.can_push,
            "can_create_pr": obj.can_create_pr,
        }
        return cls(**data)


class GitRepositoryCreate(GitRepositoryBase):
    ssh_key: Optional[str] = None  # Will be encrypted before storage
    access_token: Optional[str] = None  # PAT for HTTPS + API
    access_token_username: Optional[str] = None  # For Bitbucket Cloud


class GitRepositoryUpdate(BaseModel):
    provider: Optional[str] = None
    repo_url: Optional[str] = None
    branch: Optional[str] = None
    ssh_key: Optional[str] = None
    access_token: Optional[str] = None
    access_token_username: Optional[str] = None
    is_active: Optional[bool] = None
    auto_publish: Optional[bool] = None
    default_load_mode: Optional[str] = None
    custom_host: Optional[str] = None
    write_enabled: Optional[bool] = None

    class Config:
        from_attributes = True


class GitRepositoryInDB(GitRepositoryBase):
    id: str
    ssh_key: Optional[str]  # Encrypted SSH key
    access_token: Optional[str]  # Encrypted PAT

    class Config:
        from_attributes = True
