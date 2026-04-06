from typing import List, Optional
from pydantic import BaseModel, Field


class SearchInstructionsInput(BaseModel):
    """Input schema for search_instructions tool.

    Searches existing organization instructions by keyword and/or filters.
    Use this before creating a new instruction to check for duplicates,
    or to find a related instruction to edit instead of creating one.
    """

    search: Optional[str] = Field(
        None,
        description=(
            "Single keyword (1-3 tokens). Case-insensitive substring match against "
            "instruction text and title. Use this for simple lookups; prefer `keywords` "
            "or `regex` for thorough exploration. Leave empty to list all."
        ),
        max_length=80,
    )

    keywords: Optional[List[str]] = Field(
        None,
        description=(
            "List of short keywords/phrases (1-3 tokens each) to search for. "
            "An instruction matches if ANY keyword appears in its text or title "
            "(case-insensitive substring OR). Prefer this over `search` when you "
            "want to explore multiple angles of the same topic — e.g. for a clarified "
            "'album revenue' term pass ['album', 'revenue', 'invoiceline', 'sales', "
            "'black-elephant']. Casts a wide net in one call."
        ),
        max_length=10,
    )

    regex: Optional[str] = Field(
        None,
        description=(
            "Optional regular expression (Python `re` syntax, case-insensitive) matched "
            "against instruction text and title. Use when keywords are not precise enough "
            "— e.g. `revenue\\s*>\\s*\\$?\\d+` or `\\b(album|track)_revenue\\b`. Evaluated "
            "in-process after the DB fetch, so keep patterns cheap. Union with `search`/"
            "`keywords` (an instruction matches if ANY of the provided filters hit)."
        ),
        max_length=200,
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
