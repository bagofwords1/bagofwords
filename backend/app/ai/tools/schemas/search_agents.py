"""Pydantic schemas for the search_agents tool."""
from typing import List, Optional

from pydantic import BaseModel, Field


class SearchAgentsInput(BaseModel):
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
    description: Optional[str] = None
    status: Optional[str] = None
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
    success: bool = Field(..., description="Whether the search succeeded")
    agents: List[SearchAgentsItem] = Field(default_factory=list)
    total: int = Field(0, description="Total agents matched (before limit)")
    message: Optional[str] = None
