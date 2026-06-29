from typing import List, Optional
from pydantic import BaseModel, Field


class SearchPromptsInput(BaseModel):
    """Input schema for search_prompts tool.

    Lists/searches existing reusable prompts visible to the caller. Use BEFORE
    create_prompt to avoid duplicates, or to find a prompt to edit.
    """

    query: Optional[List[str]] = Field(
        None,
        description=(
            "Keywords OR regex patterns (case-insensitive, unioned). A prompt matches if "
            "ANY query hits its title or text. Regex is auto-detected by metacharacters "
            "(`^$.*+?[](){}|`). Leave empty to list all visible prompts filtered by the "
            "other params."
        ),
        max_length=10,
    )

    scope: Optional[str] = Field(
        None,
        description="Filter by scope: 'agent', 'global', or 'private'.",
    )

    data_source_id: Optional[str] = Field(
        None,
        description="Filter to prompts attached to this agent (data source) ID.",
    )

    starters_only: bool = Field(
        False,
        description="When true, only return prompts flagged as conversation starters.",
    )

    limit: int = Field(
        20,
        description="Maximum number of prompts to return (1-50).",
        ge=1,
        le=50,
    )


class SearchPromptsItem(BaseModel):
    """A single prompt in the search results."""
    id: str
    title: Optional[str] = None
    text: str
    scope: Optional[str] = None
    mode: Optional[str] = None
    is_starter: Optional[bool] = None
    data_source_ids: List[str] = []
    can_manage: bool = False


class SearchPromptsOutput(BaseModel):
    """Output schema for search_prompts tool response."""

    success: bool = Field(..., description="Whether the search succeeded")
    prompts: List[SearchPromptsItem] = Field(
        default_factory=list, description="Matching prompts."
    )
    total: int = Field(0, description="Total number of matching prompts.")
    message: Optional[str] = Field(None, description="Status or error message.")
