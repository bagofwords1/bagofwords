from typing import Optional, List
from pydantic import BaseModel, Field


class ReadArtifactInput(BaseModel):
    """Input for read_artifact tool.

    - artifact_id: ID of the artifact to read (from previous create_artifact results)

    Read modes (for LONG artifacts that don't fit in context):
    - Full read (default): returns the whole code when it is small enough,
      otherwise returns a structural OUTLINE with line numbers.
    - Range read: pass offset (+ optional limit) to read a specific line range.
    - Grep read: pass grep_pattern (+ optional before/after context lines) to
      find matching lines with line numbers.
    """

    artifact_id: str = Field(
        ...,
        description="ID of the artifact to read. Find this in previous create_artifact results as 'artifact_id: <uuid>' in the conversation history."
    )
    load_screenshot: bool = Field(
        default=False,
        description="If true, include the artifact's last rendered preview screenshot in the observation images. Use this when debugging visual issues or when you need to see what the artifact currently looks like before deciding how to edit it."
    )
    include_visualization_profiles: Optional[bool] = Field(
        default=None,
        description="Include data profiles when they fit the response budget. Defaults to false for range/grep, true for full/outline. IDs are always retained; use read_query for individual profiles."
    )
    character_offset: int = Field(default=0, ge=0, description="RANGE READ: zero-based character offset within the first line. Copy next_read exactly to continue an oversized line.")
    match_offset: int = Field(default=0, ge=0, description="GREP READ: skip this many match windows; use next_read to page budget-limited results.")
    offset: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "RANGE READ: 1-based line number to start reading from. Use together with `limit` "
            "to read a slice of a LONG artifact (one whose full read returned an outline instead of code). "
            "The returned code slice is VERBATIM — safe to quote in edit_artifact find ops or edit_doc find strings."
        ),
    )
    limit: Optional[int] = Field(
        default=None,
        ge=1,
        le=2000,
        description=(
            "RANGE READ: number of lines to return starting at `offset` (default 400 when offset is set). "
            "Follow `next_read` when present; it handles both line and character continuation."
        ),
    )
    grep_pattern: Optional[str] = Field(
        default=None,
        description=(
            "GREP READ: Python regex matched against each LINE of the artifact's code (or markdown for docs). "
            "Returns matching lines with 1-based line numbers plus `before`/`after` context lines — all VERBATIM, "
            "safe to quote in edit_artifact find ops or edit_doc find strings. "
            "A line longer than 500 chars comes back as a 500-char window around each occurrence "
            "(`col_start`/`col_end`/`line_length` set); range-read the line for all of it. "
            "Use this to locate the exact region to edit in a LONG artifact without loading the whole code. "
            "Mutually exclusive with offset/limit (grep wins if both are set)."
        ),
    )
    ignore_case: bool = Field(
        default=False,
        description="GREP READ: case-insensitive matching."
    )
    before: int = Field(
        default=0,
        ge=0,
        le=20,
        description="GREP READ: number of context lines to include BEFORE each matching line (grep -B)."
    )
    after: int = Field(
        default=0,
        ge=0,
        le=20,
        description="GREP READ: number of context lines to include AFTER each matching line (grep -A)."
    )
    max_matches: int = Field(
        default=50,
        ge=1,
        le=200,
        description="GREP READ: cap on returned matches. `grep_truncated` is set when more matches exist — narrow the pattern."
    )


class ArtifactGrepMatch(BaseModel):
    """A single grep match inside the artifact's code."""

    line_no: int = Field(..., description="1-based line number of the matching line.")
    line: str = Field(..., description="The matching line, verbatim — or, for a line over 500 chars, the verbatim window line[col_start:col_end] around one occurrence.")
    before: List[str] = Field(default_factory=list, description="Context lines immediately before the match, verbatim.")
    after: List[str] = Field(default_factory=list, description="Context lines immediately after the match, verbatim.")
    context_start_line: int = Field(..., description="1-based line number of the first `before` context line (equals line_no when before is empty). Use for follow-up range reads.")
    col_start: Optional[int] = Field(default=None, description="LONG LINE: 0-based offset in the full line where `line` starts. Unset when `line` is the whole line.")
    col_end: Optional[int] = Field(default=None, description="LONG LINE: 0-based offset in the full line where `line` ends (exclusive).")
    context_omitted: bool = Field(default=False, description="Before/after lines exceeded the budget; range-read line_no for additional context.")
    line_length: Optional[int] = Field(default=None, description="LONG LINE: length of the full line.")


class ReadArtifactOutput(BaseModel):
    """Output from read_artifact tool.

    Returns the artifact's code and metadata for iteration/refinement.
    For long artifacts, `code` may be a windowed view — check `read_mode`.
    """

    artifact_id: str = Field(..., description="ID of the artifact")
    title: Optional[str] = Field(None, description="Artifact title")
    mode: str = Field(..., description="Artifact mode: 'page' or 'slides'")
    code: str = Field(..., description="The artifact code — full code for read_mode='full', the requested slice for 'range', empty for 'grep'/'outline' (see matches/outline).")
    visualization_ids: List[str] = Field(default_factory=list, description="Visualization IDs used in this artifact")
    version: int = Field(default=1, description="Current version number")
    read_mode: str = Field(default="full", description="How the code was returned: 'full' | 'range' | 'grep' | 'outline'.")
    total_lines: int = Field(default=0, description="Total lines in the artifact's full code.")
    total_chars: int = Field(default=0, description="Total characters in the artifact's full code.")
    start_line: Optional[int] = Field(default=None, description="RANGE READ: 1-based first line of the returned slice.")
    end_line: Optional[int] = Field(default=None, description="RANGE READ: 1-based last line touched by the returned slice; it can be partial. Follow next_read.")
    next_read: Optional[dict] = Field(default=None, description="Exact read_artifact arguments to continue a partial read.")
    start_character: int = Field(default=0, description="Zero-based character offset within start_line.")
    truncated: bool = Field(default=False, description="True when source remains unread, including a partial last line. Follow next_read.")
    matches: Optional[List[ArtifactGrepMatch]] = Field(default=None, description="GREP READ: matching lines with context.")
    grep_total_matches: Optional[int] = Field(default=None, description="GREP READ: total matches found (may exceed len(matches)); a long line counts once per window.")
    grep_truncated: Optional[bool] = Field(default=None, description="GREP READ: true when matches were dropped due to max_matches.")
    outline: Optional[str] = Field(default=None, description="OUTLINE: structural map of the code with line numbers (returned for long artifacts on a full read, or as a navigation aid when grep found no matches).")
