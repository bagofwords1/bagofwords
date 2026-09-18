from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, field_validator


def _clean_name(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    v = v.strip()
    if not v:
        raise ValueError("Catalog name cannot be empty")
    if len(v) > 120:
        raise ValueError("Catalog name is too long (max 120 characters)")
    return v


class AgentCatalogCreate(BaseModel):
    name: str
    description: Optional[str] = None
    color: Optional[str] = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        return _clean_name(v)


class AgentCatalogUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    color: Optional[str] = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: Optional[str]) -> Optional[str]:
        return _clean_name(v)


class AgentCatalogAgentsUpdate(BaseModel):
    """The complete set of agents that should be in the catalog."""
    data_source_ids: List[str]


class AgentCatalogAssignment(BaseModel):
    """Set one agent's catalog; None removes it from its catalog."""
    catalog_id: Optional[str] = None


class AgentCatalogSchema(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    color: Optional[str] = None
    agent_count: int = 0
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True
