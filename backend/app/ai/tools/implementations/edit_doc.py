"""edit_doc tool — edit a markdown document artifact (mode='doc').

Mirrors edit_artifact's surgical-diff-with-fallback contract, minus the coder
LLM: the planner authors replacement text itself, so the tool applies string
edits directly. Each edit call inserts a NEW artifact row (version history for
the frontend dropdown), exactly like edit_artifact.
"""
import logging
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.edit_doc import EditDocInput, EditDocOutput
from app.ai.tools.schemas.events import (
    ToolEndEvent,
    ToolEvent,
    ToolProgressEvent,
    ToolStartEvent,
)
from app.models.artifact import Artifact

from ._doc_markdown import (
    MAX_DOC_CHARS,
    DocEditError,
    apply_find_replace_edits,
    extract_viz_placeholders,
    heading_outline,
)
from .create_doc import doc_observation_snapshot, validate_doc_visualizations

logger = logging.getLogger(__name__)


class EditDocTool(Tool):
    """Edit an existing markdown document artifact via find/replace ops or full rewrite."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="edit_doc",
            description=(
                "Edit an existing document created with create_doc. Unless the doc's current markdown "
                "is already fully visible in context, call read_artifact first (it returns a doc's "
                "markdown in the `code` field) so your `find` strings quote the exact current text. "
                "PREFER surgical `edits` "
                "(find/replace ops — each `find` must match the current markdown exactly once; all ops "
                "apply atomically). For restructures too large for surgical edits, pass full `markdown` "
                "instead. Embedded {{viz:<uuid>}} placeholders are re-validated after the edit. "
                "Edits are additive by default — preserve existing sections and the title unless the "
                "user asked to change them. Only for mode='doc' documents; for dashboards use edit_artifact."
            ),
            category="action",
            version="1.0.0",
            input_schema=EditDocInput.model_json_schema(),
            output_schema=EditDocOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=60,
            idempotent=False,
            required_permissions=[],
            is_active=True,
            tags=["artifact", "doc"],
            allowed_modes=["chat", "deep"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return EditDocInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return EditDocOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = EditDocInput(**tool_input)

        yield ToolStartEvent(type="tool.start", payload={"doc_id": data.doc_id})

        has_edits = bool(data.edits)
        has_markdown = data.markdown is not None and data.markdown.strip() != ""
        if has_edits == has_markdown:  # neither, or both
            yield self._fail(
                data.doc_id,
                "Provide exactly one of `edits` (surgical find/replace) or `markdown` (full rewrite).",
            )
            return

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        user = runtime_ctx.get("user")
        organization = runtime_ctx.get("organization")
        report_id = str(report.id) if report else None

        result = await db.execute(
            select(Artifact).where(
                Artifact.id == data.doc_id,
                Artifact.deleted_at.is_(None),
            )
        )
        artifact = result.scalar_one_or_none()
        if artifact is None:
            yield self._fail(data.doc_id, f"Document {data.doc_id} not found.")
            return
        if artifact.mode != "doc":
            yield self._fail(
                data.doc_id,
                f"Artifact {data.doc_id} is a '{artifact.mode}' artifact, not a document. "
                f"Use edit_artifact for dashboards/slides.",
            )
            return
        if report_id and str(artifact.report_id) != report_id:
            yield self._fail(data.doc_id, f"Document {data.doc_id} does not belong to this report.")
            return

        current_markdown = ""
        if isinstance(artifact.content, dict):
            current_markdown = artifact.content.get("markdown") or ""

        diff_applied = False
        if has_edits:
            yield ToolProgressEvent(type="tool.progress", payload={"stage": "applying_edits"})
            try:
                ops = [op.model_dump() for op in (data.edits or [])]
                new_markdown = apply_find_replace_edits(current_markdown, ops)
                diff_applied = True
            except DocEditError as e:
                yield self._fail(data.doc_id, str(e), error_type="edit_match_error")
                return
        else:
            new_markdown = data.markdown or ""

        if not new_markdown.strip():
            yield self._fail(data.doc_id, "The edit would leave the document empty.")
            return
        if len(new_markdown) > MAX_DOC_CHARS:
            yield self._fail(
                data.doc_id,
                f"Document would be too long ({len(new_markdown)} chars; max {MAX_DOC_CHARS}).",
            )
            return

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "validating_visualizations"})
        viz_ids = extract_viz_placeholders(new_markdown)
        valid_viz_ids, problems = await validate_doc_visualizations(db, report_id, viz_ids)
        if problems:
            yield self._fail(
                data.doc_id,
                "Invalid visualization placeholders after edit: " + "; ".join(problems) + ". "
                "No new version was saved. Fix the {{viz:...}} placeholders and retry.",
                error_type="invalid_visualizations",
            )
            return

        new_version = (artifact.version or 1) + 1
        new_title = data.title or artifact.title
        new_artifact = Artifact(
            report_id=str(artifact.report_id),
            user_id=str(user.id) if user else (str(artifact.user_id) if artifact.user_id else None),
            organization_id=str(organization.id) if organization else (
                str(artifact.organization_id) if artifact.organization_id else None
            ),
            title=new_title,
            mode="doc",
            content={"markdown": new_markdown, "visualization_ids": valid_viz_ids},
            generation_prompt=None,
            version=new_version,
            status="completed",
        )
        db.add(new_artifact)
        await db.commit()
        await db.refresh(new_artifact)

        yield ToolProgressEvent(
            type="tool.progress",
            payload={"stage": "doc_edited", "doc_id": str(new_artifact.id), "timing": False},
        )

        outline = heading_outline(new_markdown)
        output = {
            "success": True,
            "doc_id": str(new_artifact.id),
            "title": new_title,
            "version": new_version,
            "visualization_ids": valid_viz_ids,
            "diff_applied": diff_applied,
        }
        observation: Dict[str, Any] = {
            "summary": (
                f"Edited document '{new_title or 'Untitled'}' (v{new_version}, doc_id: {new_artifact.id}) "
                f"via {'surgical edits' if diff_applied else 'full rewrite'}. "
                f"{len(valid_viz_ids)} embedded visualization(s)."
            ),
            "doc_id": str(new_artifact.id),
            "artifact_id": str(new_artifact.id),
            "previous_doc_id": str(artifact.id),
            "mode": "doc",
            "title": new_title,
            "version": new_version,
            "visualization_ids": valid_viz_ids,
            "diff_applied": diff_applied,
            "outline": outline,
            "markdown_snapshot": doc_observation_snapshot(new_markdown),
        }
        yield ToolEndEvent(type="tool.end", payload={"output": output, "observation": observation})

    def _fail(self, doc_id: str, message: str, error_type: str = "validation_error") -> ToolEndEvent:
        return ToolEndEvent(
            type="tool.end",
            payload={
                "output": {"success": False, "doc_id": doc_id, "error": message},
                "observation": {
                    "summary": f"Failed to edit document: {message}",
                    "doc_id": doc_id,
                    "error": {"type": error_type, "message": message},
                },
            },
        )
