from typing import Any, List, Optional
from pydantic import BaseModel, Field


class ReadInstructionInput(BaseModel):
    """Input schema for the read_instruction tool.

    Reads the full text of a single instruction or skill (advertised in
    <available_skills> / <available_instructions>) by the SHORT id prefix
    shown there.
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


class ReadInstructionOutput(BaseModel):
    """Output schema for the read_instruction tool response."""

    success: bool = Field(..., description="Whether the read succeeded")
    id: Optional[str] = Field(None, description="Full instruction id that was read")
    short_id: Optional[str] = Field(None, description="Short id (first 8 chars)")
    title: Optional[str] = Field(None, description="Instruction title")
    description: Optional[str] = Field(None, description="Instruction description, if set")
    text: Optional[str] = Field(None, description="Full instruction text")
    category: Optional[str] = Field(None, description="Instruction category")
    kind: Optional[str] = Field(None, description="'instruction' or 'skill'")
    load_mode: Optional[str] = Field(None, description="Loading mode")
    status: Optional[str] = Field(
        None,
        description=(
            "Lifecycle status of the stored row: 'published', 'draft' or 'archived'. "
            "The UI labels these Active / Inactive / Archived respectively. This is "
            "the ONLY source of truth for whether the instruction is in effect — never "
            "infer it from the fact that the read succeeded."
        ),
    )
    active: Optional[bool] = Field(
        None,
        description=(
            "True only when status == 'published'. False means the instruction is NOT "
            "applied to conversations, even though its text was returned."
        ),
    )
    message: Optional[str] = Field(None, description="Status or error message")

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
