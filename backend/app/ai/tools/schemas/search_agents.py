"""Pydantic schemas for the search_agents tool."""
from typing import List, Optional

from pydantic import BaseModel, Field


class SearchAgentsInput(BaseModel):
    directory: bool = Field(False, description="Browse agent folders. Without a filter, list visible root folders and root agents.")
    folder_id: Optional[str] = Field(None, description="List direct child folders and eligible agents of this folder ID.")
    path_glob: Optional[str] = Field(None, max_length=512, description="Match visible folder paths, e.g. Marketing/**. * matches one segment, ** any depth.")
    offset: int = Field(0, ge=0, description="Skip this many directory entries for pagination.")
    same_folder_as: Optional[str] = Field(None, description="Limit results to the visible home folder of this accessible agent ID.")
    query: Optional[List[str]] = Field(
        None,
        description=(
            "Keyword, glob (`*`/`?`), or regex terms (case-insensitive, unioned, "
            "singular/plural-forgiving) matched against each agent's name, description, "
            "primary instruction, and table/tool names. Pass 2-5 terms covering different "
            "angles of what you need. Omit to list all candidate agents. When nothing "
            "matches, the result falls back to all candidates ranked by your recent usage "
            "— pick from that list instead of searching again."
        ),
        max_length=10,
    )
    limit: int = Field(
        10, description="Max agents to return (ranked by your recent usage).", ge=1, le=30
    )
    title: Optional[str] = Field(
        None,
        description="Short active-voice status label shown to the user, e.g. 'Searching agents for revenue'.",
    )


class SearchAgentsItem(BaseModel):
    id: str
    name: str
    folder_path: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    # ok | training | development — combined with status (publish_status) by
    # the UI into the agent's lifecycle stage (Development/Training/...).
    reliability_status: Optional[str] = None
    item_kind: Optional[str] = None
    item_count: int = 0
    focused: bool = False
    # False when the agent is accessible but OUTSIDE the report's manual
    # selection — focusing it via set_report_agents will ask the user.
    attached: bool = True
    # True when the user must Connect (sign in) before this agent is usable
    # (user_required auth, e.g. PowerBI OBO, with no credentials yet).
    needs_signin: bool = False
    score: float = 0.0
    # Icon hints for the UI (mirror DataSourceIcon props): connection type, the
    # catalog/connector key when known, and any per-agent icon override token.
    type: Optional[str] = None
    connector_key: Optional[str] = None
    icon: Optional[str] = None


class SearchAgentsOutput(BaseModel):
    folders: List[dict[str, str]] = Field(default_factory=list)
    success: bool = Field(..., description="Whether the search succeeded")
    agents: List[SearchAgentsItem] = Field(default_factory=list)
    total: int = Field(0, description="Total agents matched (before limit)")
    message: Optional[str] = None
