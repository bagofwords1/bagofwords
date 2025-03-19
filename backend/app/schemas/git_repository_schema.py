from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import json


class GitRepositoryBase(BaseModel):
    provider: str  # e.g., 'github', 'gitlab', 'bitbucket'
    repo_url: str
    branch: str = "main"
    is_active: bool = True


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
    ssh_key: Optional[str]  # Will be encrypted before storage


class GitRepositoryUpdate(GitRepositoryBase):
    provider: Optional[str] = None
    repo_url: Optional[str] = None
    ssh_key: Optional[str] = None

    class Config:
        from_attributes = True


class GitRepositoryInDB(GitRepositoryBase):
    id: str
    ssh_key: Optional[str]  # Encrypted SSH key

    class Config:
        from_attributes = True
