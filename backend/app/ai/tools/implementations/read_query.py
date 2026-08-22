"""
read_query tool - Read previously created query/visualization data and metadata.

Use this to load previous create_data results into context without re-executing.
Accepts multiple query_ids and/or visualization_ids.
"""

from typing import AsyncIterator, Dict, Any, Type, List, Optional

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import lazyload, selectinload

from app.ai.data_preview import build_data_preview, clamp_stats, gate_stats_for_privacy
from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from app.ai.tools.schemas.read_query import ReadQueryInput, ReadQueryOutput, ReadQueryResult
from app.models.query import Query
from app.models.visualization import Visualization
from app.models.step import Step


# Mirrors read_artifact.FULL_READ_MAX_CHARS: below this the whole code goes to
# the planner verbatim. Generated query code is orders of magnitude smaller —
# this is a backstop, not a routine path. read_query has no range/grep read
# mode, so an oversize body is clipped with an explicit note rather than
# swapped for an outline.
FULL_CODE_MAX_CHARS = 50_000


def _observation_code(code: Optional[str]) -> Optional[str]:
    """The generated code as the planner should see it: verbatim, or clipped
    with a note when it exceeds the full-read budget."""
    if not code:
        return None
    if len(code) <= FULL_CODE_MAX_CHARS:
        return code
    return (
        code[:FULL_CODE_MAX_CHARS]
        + f"\n… [clipped at {FULL_CODE_MAX_CHARS:,} of {len(code):,} chars]"
    )


class ReadQueryTool(Tool):
    """Tool to read previously created queries/visualizations from the current report."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_query",
            description=(
                "Read previously created queries or visualizations' data, code, and config from the current report. "
                "Use this to reference earlier create_data results without re-executing the query. "
                "Accepts multiple query_ids and/or visualization_ids from the conversation history. "
                "Use cases: unsure with what viz or query to generate the dashboard, want to look at previously written code, and else. "
                "IMPORTANT: Extract the query_id or viz_id from previous tool results in the conversation — do NOT ask the user for IDs."
            ),
            category="research",
            version="1.0.0",
            input_schema=ReadQueryInput.model_json_schema(),
            output_schema=ReadQueryOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=30,
            idempotent=True,
            is_active=True,
            required_permissions=[],
            tags=["query", "visualization", "data", "read"],
            observation_policy="on_trigger",
            allowed_modes=["chat"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ReadQueryInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ReadQueryOutput

    async def _resolve_by_viz_id(
        self, db, report, organization, viz_id: str, allow_llm_see_data: bool
    ) -> ReadQueryResult:
        """Resolve a single visualization_id to a ReadQueryResult."""
        try:
            result = await db.execute(
                select(Visualization)
                .options(
                    lazyload("*"),
                    selectinload(Visualization.query).options(
                        lazyload("*"),
                        selectinload(Query.default_step).options(lazyload("*")),
                        selectinload(Query.steps).options(lazyload("*")),
                    ),
                )
                .where(
                    Visualization.id == viz_id,
                    *([Visualization.report_id == str(report.id)] if report else []),
                )
            )
            visualization = result.scalar_one_or_none()
            if not visualization:
                return ReadQueryResult(visualization_id=viz_id, error=f"Visualization not found: {viz_id}")

            # The target query and only its step versions were loaded with the
            # visualization above. Reusing it avoids a second unrestricted
            # Query select (and another traversal of Report's eager graph).
            return self._build_result(visualization.query, visualization, allow_llm_see_data)
        except Exception as e:
            return ReadQueryResult(visualization_id=viz_id, error=str(e))

    async def _resolve_by_query_id(
        self, db, report, organization, query_id: str, allow_llm_see_data: bool
    ) -> ReadQueryResult:
        """Resolve a single query_id to a ReadQueryResult."""
        try:
            result = await db.execute(
                select(Query)
                .options(
                    lazyload("*"),
                    selectinload(Query.default_step).options(lazyload("*")),
                    selectinload(Query.steps).options(lazyload("*")),
                )
                .where(
                    Query.id == query_id,
                    Query.organization_id == str(organization.id),
                )
            )
            query = result.scalar_one_or_none()
            if not query:
                return ReadQueryResult(query_id=query_id, error=f"Query not found: {query_id}")

            # Find associated visualization
            viz_result = await db.execute(
                select(Visualization)
                .options(lazyload("*"))
                .where(Visualization.query_id == str(query.id))
                .limit(1)
            )
            visualization = viz_result.scalar_one_or_none()

            return self._build_result(query, visualization, allow_llm_see_data)
        except Exception as e:
            return ReadQueryResult(query_id=query_id, error=str(e))

    def _build_result(
        self, query: Optional[Query], visualization: Optional[Visualization], allow_llm_see_data: bool
    ) -> ReadQueryResult:
        """Build a ReadQueryResult from resolved query/visualization."""
        # Resolve step: prefer default_step, then latest step from query
        step: Optional[Step] = None
        if query:
            if query.default_step:
                step = query.default_step
            elif query.steps:
                step = query.steps[-1]

        step_data = step.data if step else None
        step_code = step.code if step else None
        step_data_model = step.data_model if step else None
        step_view = step.view if step else None
        step_title = step.title if step else (query.title if query else None)

        if not step_view and visualization:
            step_view = visualization.view

        # Reuse the same budgeted preview as create_data so read_query returns the
        # full result (up to the byte budget), not a fixed 5-row slice.
        data_preview = None
        if step_data and isinstance(step_data, dict):
            data_preview = build_data_preview(step_data, allow_llm_see_data=allow_llm_see_data)

        return ReadQueryResult(
            query_id=str(query.id) if query else None,
            visualization_id=str(visualization.id) if visualization else None,
            title=step_title,
            code=step_code if allow_llm_see_data else None,
            data=step_data,
            data_preview=data_preview,
            data_model=step_data_model,
            view=step_view,
            step_id=str(step.id) if step else None,
            parameters=list(getattr(query, "parameters", None) or []) or None if query else None,
            applied_params=getattr(step, "applied_params", None) if step else None,
        )

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = ReadQueryInput(**tool_input)

        all_query_ids = data.query_ids or []
        all_viz_ids = data.visualization_ids or []

        if not all_query_ids and not all_viz_ids:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": ReadQueryOutput(
                        success=False,
                        errors=["At least one query_id or visualization_id is required"],
                    ).model_dump(),
                    "observation": {
                        "summary": "read_query failed: no IDs provided",
                        "error": {"type": "validation_error", "message": "At least one query_id or visualization_id is required"},
                    },
                },
            )
            return

        total = len(all_query_ids) + len(all_viz_ids)
        yield ToolStartEvent(type="tool.start", payload={"count": total})
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "looking_up", "count": total})

        # Get context
        context_hub = runtime_ctx.get("context_hub")
        db = context_hub.db if context_hub else runtime_ctx.get("db")
        organization = context_hub.organization if context_hub else runtime_ctx.get("organization")
        report = context_hub.report if context_hub else runtime_ctx.get("report")

        if not db or not organization:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": ReadQueryOutput(
                        success=False,
                        errors=["Missing database or organization context"],
                    ).model_dump(),
                    "observation": {
                        "summary": "read_query failed: missing context",
                        "error": {"type": "context_error", "message": "Missing db or organization"},
                    },
                },
            )
            return

        # Resolve allow_llm_see_data once
        organization_settings = runtime_ctx.get("settings")
        allow_llm_see_data = True
        if organization_settings:
            try:
                allow_llm_see_data = organization_settings.get_config("allow_llm_see_data").value
            except Exception:
                allow_llm_see_data = True

        # Resolve all IDs
        results: List[ReadQueryResult] = []

        for viz_id in all_viz_ids:
            r = await self._resolve_by_viz_id(db, report, organization, viz_id, allow_llm_see_data)
            results.append(r)

        for query_id in all_query_ids:
            r = await self._resolve_by_query_id(db, report, organization, query_id, allow_llm_see_data)
            results.append(r)

        # Determine overall success
        errors = [r.error for r in results if r.error]
        all_success = len(errors) == 0
        succeeded = [r for r in results if not r.error]

        output = ReadQueryOutput(
            success=all_success,
            results=results,
            errors=errors if errors else None,
        ).model_dump()

        # Build observation — mirror create_data's observation shape
        summary_parts = []
        all_previews = []
        for r in succeeded:
            label = f"'{r.title or 'Untitled'}'"
            if r.code:
                # Size marker in the summary, read_artifact-style: the summary
                # survives compaction, so the planner still knows the code was
                # shown (and can re-read deliberately) once the body is gone.
                label += f" (code: {len(r.code):,} chars / {len(r.code.splitlines()):,} lines)"
            summary_parts.append(label)
            if r.data_preview:
                all_previews.append(r.data_preview)

        summary = f"Read {len(succeeded)} query(ies): {', '.join(summary_parts)}." if summary_parts else "read_query: no results found."

        observation: Dict[str, Any] = {
            "summary": summary,
            "analysis_complete": False,
            "final_answer": None,
        }

        # For single result, flatten the observation like create_data does
        if len(succeeded) == 1:
            r = succeeded[0]
            observation["data_preview"] = r.data_preview
            # The generated code, exactly as read_artifact puts it in ITS
            # observation. `code` used to live only on the tool OUTPUT — which
            # goes to the UI block and the DB, never into the prompt — so the
            # tool's headline use case ("look at previously written code") did
            # not work: a question about the query itself ("why does this
            # return 0 rows?") was unanswerable from what the model received,
            # and it re-read the same query instead of answering. Available for
            # 1 iteration; compacted by the observation builder on the next
            # tool call. `_build_result` already nulls it when
            # allow_llm_see_data is off.
            if r.code:
                observation["code"] = _observation_code(r.code)
            info = r.data.get("info", {}) if r.data and isinstance(r.data, dict) else {}
            observation["stats"] = clamp_stats(info) if allow_llm_see_data else clamp_stats(gate_stats_for_privacy(info))
            if r.data_model:
                observation["data_model"] = r.data_model
            if r.view:
                observation["view"] = r.view
            if r.step_id:
                observation["step_id"] = r.step_id
            if r.parameters:
                observation["parameters"] = r.parameters
                if r.applied_params is not None:
                    observation["applied_params"] = r.applied_params
        elif succeeded:
            # Multiple results: provide a summary of each — each carrying its
            # own code, so reading N queries to compare them shows what they
            # actually run, not just their titles.
            results_summary = []
            for r in succeeded:
                entry = {
                    "title": r.title,
                    "query_id": r.query_id,
                    "visualization_id": r.visualization_id,
                    "data_model": r.data_model,
                    "data_preview": r.data_preview,
                }
                if r.code:
                    entry["code"] = _observation_code(r.code)
                if r.parameters:
                    entry["parameters"] = r.parameters
                results_summary.append(entry)
            observation["results_summary"] = results_summary

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output,
                "observation": observation,
            },
        )
