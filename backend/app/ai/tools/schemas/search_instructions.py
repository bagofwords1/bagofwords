from typing import List, Optional
from pydantic import BaseModel, Field


class SearchInstructionsInput(BaseModel):
    """Input schema for search_instructions tool.

    Searches existing organization instructions by keyword and/or filters.
    Use this before creating a new instruction to check for duplicates,
    or to find a related instruction to edit instead of creating one.
    """

    query: Optional[List[str]] = Field(
        None,
        description=(
            "List of keywords OR regex patterns to search for (case-insensitive). "
            "An instruction matches if ANY query hits its text or title. Each entry "
            "can be: "
            "(1) a plain keyword/phrase — matched as a literal substring (e.g. 'revenue', "
            "'album revenue', 'black-elephant'); or "
            "(2) a regex pattern — detected automatically by the presence of regex "
            "metacharacters (`^$.*+?[](){}|`), e.g. `revenue\\s*>\\s*\\$?\\d+`, "
            "`\\b(album|track)_revenue\\b`, `.*cancel.*`. "
            "Cast a wide net: pass 3-6 queries in ONE call covering different angles "
            "of the topic (the clarified term, the metric, the tables involved, "
            "synonyms). Leave empty to list all instructions filtered by other params."
        ),
        max_length=10,
    )

    category: Optional[str] = Field(
        None,
        description=(
            "Filter by category: 'general', 'code_gen', 'visualization', "
            "'dashboard', or 'system'."
        ),
    )

    data_source_ids: Optional[List[str]] = Field(
        None,
        description=(
            "Filter to instructions associated with these data source IDs. "
            "Useful when looking for instructions about specific tables."
        ),
    )

    limit: int = Field(
        20,
        description="Maximum number of instructions to return (1-50).",
        ge=1,
        le=50,
    )


class SearchInstructionsItem(BaseModel):
    """A single instruction in the search results."""
    id: str
    title: Optional[str] = None
    text: str
    category: Optional[str] = None
    load_mode: Optional[str] = None
    status: Optional[str] = None


class SearchInstructionsOutput(BaseModel):
    """Output schema for search_instructions tool response."""

    success: bool = Field(..., description="Whether the search succeeded")
    instructions: List[SearchInstructionsItem] = Field(
        default_factory=list,
        description="Matching instructions, ordered by relevance.",
    )
    total: int = Field(
        0,
        description="Total number of matching instructions (may exceed returned count).",
    )
    message: Optional[str] = Field(
        None,
        description="Status or error message.",
    )
