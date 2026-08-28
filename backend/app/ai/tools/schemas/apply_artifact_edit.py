from typing import List, Optional
from pydantic import BaseModel, Field


class ArtifactEditOp(BaseModel):
    """One exact find/replace operation. `find` must match the current code
    EXACTLY ONCE (whitespace included). Keep finds as short as possible while
    still unique."""

    find: str = Field(..., description="Exact text to locate in the current artifact code — must occur exactly once.")
    replace: str = Field(..., description="Replacement text (empty string deletes the found text).")


class ApplyArtifactEditInput(BaseModel):
    """Input for apply_artifact_edit — the MECHANICAL artifact edit path.

    YOU author the exact code edits (no second model involved): the tool
    applies them atomically, enforces the viz-reference and params-wiring
    contracts, render-validates once, and persists a new version. If any op
    fails to match, NOTHING is applied and the error names the closest match
    so you can correct the find text.
    """

    artifact_id: str = Field(..., description="Id of the page-mode artifact to edit.")
    edits: List[ArtifactEditOp] = Field(..., min_length=1, description="Ordered find/replace operations, applied atomically (all or none).")
    visualization_ids: Optional[List[str]] = Field(default=None, description="NEW visualization ids to add to the artifact's data payload (existing ones are kept automatically). Your edits must add code sections rendering them via vizById(\"<uuid>\").")
    remove_visualization_ids: Optional[List[str]] = Field(default=None, description="Visualization ids to REMOVE from the payload. Your edits must delete every code section referencing them.")
    title: Optional[str] = Field(default=None, description="Updated artifact title (kept if omitted).")


class ApplyArtifactEditOutput(BaseModel):
    """Output from apply_artifact_edit."""

    success: bool = Field(...)
    artifact_id: str = Field(...)
    version: Optional[int] = Field(default=None)
    applied_ops: Optional[int] = Field(default=None)
    error: Optional[str] = Field(default=None)
