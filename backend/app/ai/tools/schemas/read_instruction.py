from typing import Any, List, Optional
from pydantic import BaseModel, Field


class ReadInstructionInput(BaseModel):
    """Input schema for the read_instruction tool.

    Reads the full text of a single instruction or skill (advertised in
    <available_skills> / <available_instructions>) by the SHORT id prefix
    shown there. Long bodies are read in windows: a line range
    (offset/limit) or a line-level grep (grep_pattern + context lines).
    """

    id: str = Field(
        ...,
        description=(
            "The instruction/skill id — pass the SHORT id prefix exactly as shown "
            "in <available_skills> or <available_instructions> (e.g. 'be8090f2'). "
            "The full UUID also works. Must be at least 4 characters; if the "
            "prefix matches more than one entry you'll get the candidates back, "
            "so pass a longer prefix."
        ),
        min_length=4,
    )
    offset: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "RANGE READ: 1-based line number to start reading from. Use with `limit` "
            "to read a slice of a LONG instruction (one whose plain read came back with "
            "`truncated: true`). The slice is VERBATIM — safe to anchor edit_instruction on."
        ),
    )
    limit: Optional[int] = Field(
        default=None,
        ge=1,
        le=2000,
        description=(
            "RANGE READ: number of lines to return starting at `offset` (default 400). "
            "Very long lines can end the window earlier — continue from `next_offset`."
        ),
    )
    grep_pattern: Optional[str] = Field(
        default=None,
        description=(
            "GREP READ: Python regex matched against each LINE of the instruction text, "
            "e.g. 'connect|pair|weight'. Returns matching lines with 1-based line numbers "
            "plus `before`/`after` context lines, all VERBATIM. Use it to pull the entries "
            "you need out of a long catalogue instead of reading all of it. "
            "A line over 500 chars comes back as one window per occurrence "
            "(`col_start`/`col_end` locate it in the line). "
            "Takes precedence over offset/limit."
        ),
    )
    ignore_case: bool = Field(
        default=True,
        description="GREP READ: case-insensitive matching (default true).",
    )
    before: int = Field(
        default=0,
        ge=0,
        le=20,
        description="GREP READ: context lines to include BEFORE each match (grep -B).",
    )
    after: int = Field(
        default=0,
        ge=0,
        le=20,
        description="GREP READ: context lines to include AFTER each match (grep -A).",
    )
    max_matches: int = Field(
        default=50,
        ge=1,
        le=200,
        description="GREP READ: cap on returned matches. `grep_truncated` is set when more exist — narrow the pattern.",
    )


class ReadInstructionOutput(BaseModel):
    """Output schema for the read_instruction tool response."""

    success: bool = Field(..., description="Whether the read succeeded")
    id: Optional[str] = Field(None, description="Full instruction id that was read")
    short_id: Optional[str] = Field(None, description="Short id (first 8 chars)")
    title: Optional[str] = Field(None, description="Instruction title")
    description: Optional[str] = Field(None, description="Instruction description, if set")
    text: Optional[str] = Field(
        None,
        description=(
            "Instruction text: the whole body on a full read; the requested lines on a "
            "range read; the opening lines on a plain read of a body too long to return "
            "whole (see `truncated`); empty on a grep read (see `matches`)."
        ),
    )
    category: Optional[str] = Field(None, description="Instruction category")
    kind: Optional[str] = Field(None, description="'instruction' or 'skill'")
    load_mode: Optional[str] = Field(None, description="Loading mode")
    message: Optional[str] = Field(None, description="Status or error message")

    read_mode: Optional[str] = Field(
        None, description="'full' | 'head' (body too long, opening lines + outline) | 'range' | 'grep'"
    )
    total_chars: Optional[int] = Field(None, description="Length of the whole instruction text")
    total_lines: Optional[int] = Field(None, description="Line count of the whole instruction text")
    start_line: Optional[int] = Field(None, description="First line returned in `text` (range/head reads)")
    end_line: Optional[int] = Field(None, description="Last line returned in `text` (range/head reads)")
    truncated: Optional[bool] = Field(
        None, description="True when lines after `end_line` were not returned — read on from `next_offset`."
    )
    next_offset: Optional[int] = Field(None, description="Pass as `offset` to continue reading")
    outline: Optional[str] = Field(
        None,
        description="Line-numbered outline (headings / keys / list entries) of a long body, for picking a range or a grep pattern.",
    )
    matches: Optional[List[Any]] = Field(None, description="GREP READ: matching lines with context")
    grep_total_matches: Optional[int] = Field(None, description="GREP READ: total matches found")
    grep_truncated: Optional[bool] = Field(None, description="GREP READ: more matches exist than were returned")

    pending_changes: Optional[List[Any]] = Field(
        None,
        description=(
            "Suggested edits awaiting review on this instruction, newest first — each with "
            "the build that proposed it, who proposed it and when, and a preview of what it "
            "changes. INFORMATIONAL ONLY. `text` above is the live instruction and the only "
            "text an edit_instruction anchor may match; pending text is not part of it and "
            "anchoring on it fails. Use this to notice that a change you are about to propose "
            "is already proposed, and say so instead of stacking a second one."
        ),
    )
