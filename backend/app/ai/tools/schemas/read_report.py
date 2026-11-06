from typing import List, Optional
from pydantic import BaseModel, Field


class ReadReportInput(BaseModel):
    """Input schema for the ``read_report`` tool.

    Reads the metadata, conversation, and artifact/data-source summary of a
    single report owned by the current user. Use ``search_reports`` first to
    find the ``report_id``. Access is restricted to the caller's own reports.
    """

    report_id: str = Field(
        ...,
        description="The id of the report to read (from search_reports results).",
    )

    include_conversation: bool = Field(
        True,
        description="Include the report's conversation messages (prompts and AI answers).",
    )

    offset: Optional[int] = Field(
        None,
        ge=0,
        description=(
            "PAGINATION: 0-based index of the first conversation message to return "
            "(messages are numbered oldest-first; each returned message carries its "
            "`index`). Omit to get the MOST RECENT page. To read a long report "
            "progressively, start at 0 and keep passing `next_offset` until "
            "`has_more` is false; to step back from the latest page pass `prev_offset`."
        ),
    )

    limit: int = Field(
        20,
        ge=1,
        le=50,
        description="PAGINATION: number of conversation messages per page (1-50, default 20).",
    )

    max_content_chars: int = Field(
        2000,
        ge=200,
        le=20000,
        description=(
            "Per-message character cap. A message cut at this cap comes back with "
            "`truncated: true` and its full `content_chars`; re-read it whole with "
            "offset=<its index>, limit=1 and a larger max_content_chars, or continue "
            "it with `content_offset`."
        ),
    )

    content_offset: int = Field(
        0,
        ge=0,
        description=(
            "Character offset into each returned message's text (use with limit=1 to "
            "read the rest of one long, truncated message: pass its `next_content_offset`)."
        ),
    )


class ReadReportMessage(BaseModel):
    index: int = Field(..., description="0-based position in the full conversation (oldest first)")
    role: str
    content: str
    created_at: Optional[str] = None
    content_chars: int = Field(0, description="Length of the message's full text")
    truncated: bool = Field(False, description="True when `content` does not reach the end of the message")
    next_content_offset: Optional[int] = Field(
        None, description="Pass as `content_offset` (with offset=index, limit=1) to read on"
    )


class ReadReportPage(BaseModel):
    offset: int = Field(0, description="Index of the first message in this page")
    limit: int = 20
    returned: int = 0
    total_messages: int = 0
    has_more: bool = Field(False, description="Messages exist after this page — read on from `next_offset`")
    next_offset: Optional[int] = None
    has_earlier: bool = Field(False, description="Messages exist before this page — see `prev_offset`")
    prev_offset: Optional[int] = None


class ReadReportArtifact(BaseModel):
    id: str
    title: Optional[str] = None
    mode: Optional[str] = None
    version: Optional[int] = None


class ReadReportOutput(BaseModel):
    success: bool
    report_id: str
    title: Optional[str] = None
    slug: Optional[str] = None
    status: Optional[str] = None
    mode: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    data_sources: List[str] = Field(default_factory=list)
    artifacts: List[ReadReportArtifact] = Field(default_factory=list)
    conversation: List[ReadReportMessage] = Field(default_factory=list)
    page: Optional[ReadReportPage] = None
    message: Optional[str] = None
    error: Optional[str] = None
