"""Pydantic schemas for the search_agents tool."""
from typing import List, Optional

from pydantic import BaseModel, Field


class SearchAgentsInput(BaseModel):
    query: Optional[List[str]] = Field(
        None,
        description=(
            "Keyword or regex terms (case-insensitive, unioned) matched against each "
            "agent's name, description, primary instruction, and table/tool names. "
            "Pass 2-5 terms covering different angles of what you need. Omit to list "
            "all candidate agents."
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
    score: float = 0.0


class SearchAgentsOutput(BaseModel):
    success: bool = Field(..., description="Whether the search succeeded")
    agents: List[SearchAgentsItem] = Field(default_factory=list)
    total: int = Field(0, description="Total agents matched (before limit)")
    message: Optional[str] = None
