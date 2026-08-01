"""create_doc tool — author a markdown document artifact (mode='doc').

Unlike create_artifact (which delegates JSX codegen to the dashboard designer),
the planner authors the document markdown DIRECTLY in the tool args. The tool
validates embedded visualization placeholders, persists the Artifact row, and
returns an outline-level observation. No second LLM call, no sandbox render.
"""
import logging
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple, Type

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.create_doc import CreateDocInput, CreateDocOutput
from app.ai.tools.schemas.events import (
    ToolEndEvent,
    ToolEvent,
    ToolProgressEvent,
    ToolStartEvent,
)
from app.models.artifact import Artifact
from app.models.visualization import Visualization

from ._doc_markdown import (
    MAX_DOC_CHARS,
    extract_file_placeholders,
    extract_viz_placeholders,
    heading_outline,
)
from app.models.file import File

logger = logging.getLogger(__name__)

# Observation carries a snapshot of the markdown so follow-up edit_doc calls can
# quote exact text. Larger docs are truncated — the planner can read_artifact.
_OBSERVATION_MARKDOWN_LIMIT = 4000


async def validate_doc_visualizations(
    db: Any,
    report_id: Optional[str],
    viz_ids: List[str],
) -> Tuple[List[str], List[str]]:
    """Validate embedded viz ids: exist, belong to the report, successful step.

    Returns (valid_ids_in_document_order, problems). A doc with ANY invalid viz
    is rejected — a report with a broken chart is a broken deliverable.
    """
    if not viz_ids:
        return [], []

    from app.models.query import Query

    problems: List[str] = []
    try:
        result = await db.execute(
            select(Visualization)
            .options(
                selectinload(Visualization.query).selectinload(Query.default_step),
                selectinload(Visualization.query).selectinload(Query.steps),
            )
            .where(Visualization.id.in_(viz_ids))
            .execution_options(populate_existing=True)
        )
        fetched = {str(v.id).lower(): v for v in result.scalars().all()}
    except Exception as e:
        logger.exception("create_doc: failed to fetch visualizations")
        return [], [f"Error fetching visualizations: {e}"]

    valid: List[str] = []
    for viz_id in viz_ids:
        viz = fetched.get(viz_id.lower())
        if viz is None:
            problems.append(f"Visualization {viz_id} not found")
            continue
        if report_id and str(viz.report_id) != report_id:
            problems.append(f"Visualization {viz_id} does not belong to this report")
            continue
        step = None
        if viz.query and viz.query.default_step:
            step = viz.query.default_step
        elif viz.query and viz.query.steps:
            step = viz.query.steps[-1]
        status = step.status if step else None
        if status != "success":
            problems.append(
                f"Visualization {viz_id} has step status '{status or 'unknown'}' (not success)"
            )
            continue
        valid.append(str(viz.id))
    return valid, problems


def doc_observation_snapshot(markdown: str) -> str:
    if len(markdown) <= _OBSERVATION_MARKDOWN_LIMIT:
        return markdown
    return (
        markdown[:_OBSERVATION_MARKDOWN_LIMIT]
        + "\n...[document truncated — use read_artifact to fetch the full markdown]"
    )


class CreateDocTool(Tool):
    """Create a markdown document artifact authored directly by the planner."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_doc",
            description=(
                "Create a written document (report, analysis, memo) as a markdown artifact. "
                "YOU author the full markdown directly — polished analytical prose with citations. "
                "Embed live charts with {{viz:<uuid>}} placeholders (viz_ids from previous create_data "
                "results), diagrams with ```mermaid fences, and multi-column sections with ::: columns. "
                "Use this for written deliverables: root-cause analyses, deep-dive reports, executive "
                "summaries, data audits. For an interactive dashboard, use create_artifact instead. "
                "To modify an existing document, use edit_doc. "
                "CITATIONS ARE MANDATORY: every number, trend, or conclusion names its source — "
                "table/column queried, the embedded viz, and the time range; distinguish 'data shows X' "
                "from 'inferred X' and state confidence and limitations. Charts render live — never "
                "paste a chart's rows as a markdown table beside it; create the data FIRST, then the doc. "
                "Structure follows the genre — Root-cause: Symptom (with viz) → Hypotheses → Evidence per "
                "hypothesis (cited, incl. ruled-out) → Root cause → Recommended actions (mermaid for the "
                "causal chain). Deep-dive: Executive summary (3-5 bullets, numbers inline) → Findings "
                "(chart + prose + citation each) → Methodology → Next questions. Executive memo: answer "
                "first, one viz, caveats footnoted. Data audit: Scope → Checks → Issues (each with "
                "evidence) → Severity and fixes. "
                "Mermaid: wrap node labels containing punctuation in double quotes — "
                'E["revenue SUM(Invoice.Total)"], never E[revenue SUM(Invoice.Total)] — or the diagram '
                "fails to render. Multi-column: ::: columns ... ::: col ... :::."
            ),
            category="action",
            version="1.0.0",
            input_schema=CreateDocInput.model_json_schema(),
            output_schema=CreateDocOutput.model_json_schema(),
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
        return CreateDocInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreateDocOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = CreateDocInput(**tool_input)

        yield ToolStartEvent(type="tool.start", payload={"title": data.title or "Document"})

        markdown = data.markdown or ""
        if not markdown.strip():
            yield self._fail("Document markdown is empty. Provide the full document body.")
            return
        if len(markdown) > MAX_DOC_CHARS:
            yield self._fail(
                f"Document is too long ({len(markdown)} chars; max {MAX_DOC_CHARS}). "
                f"Tighten the prose — charts should carry the detail."
            )
            return

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        user = runtime_ctx.get("user")
        organization = runtime_ctx.get("organization")
        report_id = str(report.id) if report else None

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "validating_visualizations"})
        viz_ids = extract_viz_placeholders(markdown)
        valid_viz_ids, problems = await validate_doc_visualizations(db, report_id, viz_ids)
        if problems:
            yield self._fail(
                "Invalid visualization placeholders: " + "; ".join(problems) + ". "
                "Fix or remove these {{viz:...}} placeholders (use viz_ids from successful "
                "create_data results) and retry.",
                error_type="invalid_visualizations",
            )
            return

        # Embedded images. Like viz placeholders these are derived from the
        # markdown rather than passed separately, so the reference and the
        # embed can never drift apart. An id the org doesn't own is dropped
        # rather than failing the doc — the prose still stands without the art.
        file_ids = extract_file_placeholders(markdown)
        valid_file_ids: List[str] = []
        if file_ids and organization is not None:
            rows = await db.execute(
                select(File.id).where(
                    File.id.in_(file_ids),
                    File.organization_id == str(organization.id),
                )
            )
            owned = {str(r) for r in rows.scalars().all()}
            valid_file_ids = [f for f in file_ids if f in owned]

        artifact = Artifact(
            report_id=report_id,
            user_id=str(user.id) if user else None,
            organization_id=str(organization.id) if organization else None,
            title=data.title or "Untitled Document",
            mode="doc",
            content={
                "markdown": markdown,
                "visualization_ids": valid_viz_ids,
                "file_ids": valid_file_ids,
            },
            generation_prompt=None,
            version=1,
            status="completed",
        )
        db.add(artifact)
        await db.commit()
        await db.refresh(artifact)

        yield ToolProgressEvent(
            type="tool.progress",
            payload={"stage": "doc_created", "doc_id": str(artifact.id), "timing": False},
        )

        outline = heading_outline(markdown)
        output = {
            "success": True,
            "doc_id": str(artifact.id),
            "title": artifact.title,
            "version": artifact.version,
            "visualization_ids": valid_viz_ids,
            "outline": outline,
        }
        observation: Dict[str, Any] = {
            "summary": (
                f"Created document '{artifact.title}' (doc_id: {artifact.id}, v1) with "
                f"{len(valid_viz_ids)} embedded visualization(s) and {len(outline)} section heading(s)."
            ),
            "doc_id": str(artifact.id),
            "artifact_id": str(artifact.id),
            "mode": "doc",
            "title": artifact.title,
            "version": 1,
            "visualization_ids": valid_viz_ids,
            "outline": outline,
            "markdown_snapshot": doc_observation_snapshot(markdown),
        }
        yield ToolEndEvent(type="tool.end", payload={"output": output, "observation": observation})

    def _fail(self, message: str, error_type: str = "validation_error") -> ToolEndEvent:
        return ToolEndEvent(
            type="tool.end",
            payload={
                "output": {"success": False, "error": message},
                "observation": {
                    "summary": f"Failed to create document: {message}",
                    "error": {"type": error_type, "message": message},
                },
            },
        )
