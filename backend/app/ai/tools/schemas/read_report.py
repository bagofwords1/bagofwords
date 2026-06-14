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


class ReadReportMessage(BaseModel):
    role: str
    content: str
    created_at: Optional[str] = None


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
    message: Optional[str] = None
    error: Optional[str] = None
