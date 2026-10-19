"""Request/response for cloning an agent (POST /data_sources/{id}/clone)."""
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.data_source_schema import DataSourceSchema

# copy: the new agent gets its own copy, independent from then on.
# link: both agents use the same rows; a change on one shows on the other.
# none: the new agent starts without it.
# default (access only): what a newly created agent gets.


class AgentCloneIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    instructions: Literal["copy", "link", "none"] = "copy"
    # Linking saved queries waits for per-agent query runs (#1191): today a
    # linked query would keep running against the source agent.
    saved_queries: Literal["copy", "none"] = "none"
    lists: Literal["copy", "link", "none"] = "copy"
    evals: Literal["copy", "none"] = "copy"
    files: Literal["link", "none"] = "link"
    access: Literal["copy", "default"] = "default"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("name is required")
        return v


class CloneSkipped(BaseModel):
    id: str
    name: Optional[str] = None
    reason: str


class CloneComponent(BaseModel):
    copied: int = 0
    linked: int = 0
    skipped: List[CloneSkipped] = []


class AgentCloneOut(BaseModel):
    data_source: DataSourceSchema
    # Per component (tables, tool_policies, prompts, files, access,
    # instructions, saved_queries, lists, evals): what was carried over and
    # what was left out, and why.
    summary: Dict[str, CloneComponent]
