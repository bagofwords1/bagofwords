from typing import List, Optional
from pydantic import BaseModel, Field


class DocEditOp(BaseModel):
    """A single surgical find/replace edit on the document markdown."""

    find: str = Field(..., description=(
        "Exact text to find in the current document markdown. Must match EXACTLY ONCE — include enough "
        "surrounding context to make the match unique. Whitespace and newlines are matched literally."
    ))
    replace: str = Field(..., description="Replacement text (may be empty to delete the matched text).")


class EditDocInput(BaseModel):
    """Input for edit_doc tool.

    Provide EITHER `edits` (surgical find/replace, preferred) OR `markdown` (full rewrite fallback).
    """

    doc_id: str = Field(..., description=(
        "ID of the existing document to edit. Find this in previous create_doc/edit_doc results as "
        "'doc_id: <uuid>'."
    ))
    edits: Optional[List[DocEditOp]] = Field(default=None, description=(
        "Surgical find/replace edits (PREFERRED for focused changes). Each `find` must match the current "
        "markdown exactly once. All edits apply atomically — if any op fails, none are applied and the "
        "error names the failing op so you can retry with corrected context."
    ))
    markdown: Optional[str] = Field(default=None, description=(
        "Full replacement markdown (fallback for restructures too large for surgical edits). "
        "Same authoring rules as create_doc: {{viz:<uuid>}} embeds, ```mermaid fences, ::: columns, "
        "citations for every claim. Provide either `edits` or `markdown`, not both."
    ))
    title: Optional[str] = Field(default=None, description="Updated title. If not provided, the existing title is kept.")


class EditDocOutput(BaseModel):
    """Output from edit_doc tool."""

    doc_id: str = Field(..., description="ID of the edited document")
    title: Optional[str] = Field(None, description="Document title after the edit")
    version: int = Field(..., description="Bumped version number")
    visualization_ids: List[str] = Field(default_factory=list, description=(
        "All visualization IDs embedded in the document after the edit."
    ))
    diff_applied: bool = Field(..., description=(
        "True if the edit was applied as surgical find/replace ops. False if a full markdown rewrite was used."
    ))
