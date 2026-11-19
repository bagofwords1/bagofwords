"""Request/response for cloning an agent (POST /data_sources/{id}/clone)."""
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.data_source_schema import DataSourceSchema


class AgentCloneIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    # copy: the new agent gets its own copies, independent from then on.
    # link: the same instructions, now used by both agents.
    # none: the new agent starts without the source's instructions.
    instructions: Literal["copy", "link", "none"] = "copy"
    # same: the source's members and groups, with their permissions.
    # only_me: the person cloning only (it can be shared later).
    access: Literal["same", "only_me"] = "only_me"

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
    # Per component (tables, tool_policies, prompts, files, access, folder,
    # instructions): what was carried over and what was left out, and why.
    summary: Dict[str, CloneComponent]
