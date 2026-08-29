from typing import Any, List, Optional
from pydantic import BaseModel, Field, model_validator


class ArtifactEditOp(BaseModel):
    """One exact find/replace operation. `find` must match the current code
    EXACTLY ONCE (whitespace included). Keep finds as short as possible while
    still unique."""

    find: str = Field(..., description="Exact text to locate in the current artifact code — must occur exactly once.")
    replace: str = Field(..., description="Replacement text (empty string deletes the found text).")


class EditArtifactInput(BaseModel):
    """Input for edit_artifact — the MECHANICAL artifact edit path.

    YOU author the exact code edits (no second model involved): the tool
    applies them atomically, enforces the viz-reference and params-wiring
    contracts, render-validates once, and persists a new version. If any op
    fails to match, NOTHING is applied and the error names the closest match
    so you can correct the find text.
    """

    artifact_id: str = Field(..., description="Id of the page-mode artifact to edit.")
    edits: List[ArtifactEditOp] = Field(..., min_length=1, description=(
        "Ordered find/replace operations, applied atomically (all or none).\n\n"
        "PERSONALIZATION: personalization is a RUNTIME BINDING, never resolved text. When an edit touches a personalized "
        "greeting/title/section, keep it bound to `current_user` (with a neutral fallback) — NEVER substitute the requester's "
        "actual name/email into the code. The artifact is shared: every viewer gets their own identity at render time. If a "
        "preview/screenshot appears to lack personalization, that preview renders as an ANONYMOUS viewer — the fix is NEVER "
        "a literal name."
    ))
    visualization_ids: Optional[List[str]] = Field(default=None, description="NEW visualization ids to add to the artifact's data payload (existing ones are kept automatically). Your edits must add code sections rendering them via vizById(\"<uuid>\").")
    remove_visualization_ids: Optional[List[str]] = Field(default=None, description="Visualization ids to REMOVE from the payload. Your edits must delete every code section referencing them.")
    title: Optional[str] = Field(default=None, description="Updated artifact title (kept if omitted).")

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_shape(cls, data: Any) -> Any:
        """Bridge for stale patterns: the pre-mechanical edit_artifact took an
        English `edit_prompt`. Reject it with guidance instead of a confusing
        missing-field error, so a model imitating old history self-corrects."""
        if isinstance(data, dict) and data.get("edit_prompt") and not data.get("edits"):
            raise ValueError(
                "edit_artifact no longer takes an English edit_prompt — YOU author the "
                "edit: pass `edits` as exact find/replace ops against the current code "
                "in <current_artifact>.<code> (see the ARTIFACT AUTHORING REFERENCE). "
                "Call read_artifact first if the code is not in your context."
            )
        return data


class EditArtifactOutput(BaseModel):
    """Output from edit_artifact."""

    success: bool = Field(...)
    artifact_id: str = Field(...)
    version: Optional[int] = Field(default=None)
    applied_ops: Optional[int] = Field(default=None)
    error: Optional[str] = Field(default=None)