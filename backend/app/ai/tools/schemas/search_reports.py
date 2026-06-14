from typing import List, Literal, Optional
from pydantic import BaseModel, Field


class SearchReportsInput(BaseModel):
    """Input schema for the ``search_reports`` tool.

    Lists/searches reports owned by the current user. Use this to find a
    prior report — by title — before referencing or reading it with
    ``read_report``. Results are always scoped to the calling user's own
    reports; reports owned by other users are never returned.
    """

    query: Optional[str] = Field(
        None,
        description=(
            "Substring to match (case-insensitive) against the report title. "
            "Leave empty to list the user's most recent reports."
        ),
        max_length=400,
    )

    status: Literal["draft", "published", "all"] = Field(
        "all",
        description="Filter by report status. ``all`` includes drafts and published reports.",
    )

    mode: Optional[Literal["chat", "deep", "training"]] = Field(
        None,
        description="Optionally restrict to reports created in a specific mode.",
    )

    limit: int = Field(
        10,
        ge=1,
        le=50,
        description="Maximum number of reports to return (1-50).",
    )


class SearchReportsItem(BaseModel):
    id: str
    title: str
    slug: Optional[str] = None
    status: str
    mode: Optional[str] = None
    has_artifacts: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SearchReportsOutput(BaseModel):
    success: bool
    reports: List[SearchReportsItem] = Field(default_factory=list)
    total: int = 0
    search_query: Optional[str] = None
    message: Optional[str] = None
