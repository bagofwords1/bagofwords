from typing import List, Optional
from pydantic import BaseModel, Field


class NoteEditOp(BaseModel):
    """A single surgical find/replace edit on the note content."""

    find: str = Field(..., description=(
        "Exact text to find in the current note content. Must match EXACTLY ONCE — include "
        "enough surrounding context to make it unique. Whitespace/newlines matched literally."
    ))
    replace: str = Field(..., description="Replacement text (may be empty to delete the matched text).")


class EditNoteInput(BaseModel):
    """Input for edit_note tool.

    Provide EITHER `edits` (surgical find/replace, preferred) OR `content` (full replacement).
    """

    note_id: str = Field(..., description="ID of the note to edit (from the <notes> block or create_note).")
    edits: Optional[List[NoteEditOp]] = Field(default=None, description=(
        "Surgical find/replace edits (PREFERRED). Each `find` must match the current note exactly "
        "once; all edits apply atomically (if any fails, none are applied). Ideal for checking off "
        "a todo: find '- [ ] step' → replace '- [x] step'."
    ))
    content: Optional[str] = Field(default=None, description=(
        "Full replacement content (fallback for large rewrites). Provide either `edits` or `content`."
    ))
    title: Optional[str] = Field(default=None, description="Updated title. If omitted, the existing title is kept.")


class EditNoteOutput(BaseModel):
    """Output from edit_note tool."""

    note_id: str = Field(..., description="ID of the edited note")
    title: Optional[str] = Field(None, description="Note title after the edit")
    diff_applied: bool = Field(..., description="True if surgical find/replace was used, False for full replacement.")
