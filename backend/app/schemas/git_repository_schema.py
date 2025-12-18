from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import json


class GitRepositoryBase(BaseModel):
    provider: str  # e.g., 'github', 'gitlab', 'bitbucket'
    repo_url: str
    branch: str = "main"
    is_active: bool = True
    auto_publish: bool = False  # Auto-publish synced instructions
    default_load_mode: str = "intelligent"  # always, intelligent, disabled


class GitRepositorySchema(GitRepositoryBase):
    id: str
    user_id: str
    organization_id: str
    data_source_id: Optional[str]
    last_indexed_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    status: Optional[str] = None
    class Config:
        from_attributes = True


class GitRepositoryCreate(GitRepositoryBase):
    ssh_key: Optional[str] = None  # Will be encrypted before storage


class GitRepositoryUpdate(BaseModel):
    provider: Optional[str] = None
    repo_url: Optional[str] = None
    branch: Optional[str] = None
    ssh_key: Optional[str] = None
    is_active: Optional[bool] = None
    auto_publish: Optional[bool] = None
    default_load_mode: Optional[str] = None

    class Config:
        from_attributes = True


class GitRepositoryInDB(GitRepositoryBase):
    id: str
    ssh_key: Optional[str]  # Encrypted SSH key

    class Config:
        from_attributes = True
