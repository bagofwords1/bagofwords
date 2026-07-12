from typing import Optional
from pydantic import BaseModel, Field


class CreateNoteInput(BaseModel):
    """Input for create_note tool."""

    content: str = Field(..., description=(
        "The note body in markdown — YOUR working memory for this report. Write anything "
        "worth remembering as you work: a plan (use `- [ ]` / `- [x]` task lists), findings "
        "(cite the table/column/viz), ruled-out hypotheses, or definitions you're pinning down. "
        "Notes are shown back to you every step and are visible to the user, but they are NOT "
        "user instructions — they are your own notes and may be revised."
    ))
    title: Optional[str] = Field(None, description="Optional short title (e.g. 'Plan', 'Findings').")


class CreateNoteOutput(BaseModel):
    """Output from create_note tool."""

    note_id: str = Field(..., description="ID of the created note (use it with edit_note).")
    title: Optional[str] = Field(None, description="Note title")
