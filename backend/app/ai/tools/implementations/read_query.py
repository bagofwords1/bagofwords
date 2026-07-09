"""
read_query tool - Read previously created query/visualization data and metadata.

Use this to load previous create_data results into context without re-executing.
Accepts multiple query_ids and/or visualization_ids.
"""

from typing import AsyncIterator, Dict, Any, Type, List, Optional

from pydantic import BaseModel
from sqlalchemy import select

from app.ai.context.data_preview import build_data_preview, gate_stats_for_privacy
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
                "SLICE MODE: when a create_data observation included an 'artifact' block (large result retained in full), "
                "pass artifact_id (or the query_id) with offset/limit to page, match (regex) to grep, columns to project, "
                "time_from/time_to for a time window, or sql (single SELECT over table `data`) to aggregate — the FULL "
                "result is explorable in bounded pages even when the preview was truncated. "
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
            allowed_modes=["chat", "deep"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ReadQueryInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ReadQueryOutput

    async def _run_slice(
        self, data: "ReadQueryInput", runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        """Slice an artifact (full retained result) — page/grep/project/window/SQL."""
        from app.services.artifact_store import ArtifactStoreService

        context_hub = runtime_ctx.get("context_hub")
        db = context_hub.db if context_hub else runtime_ctx.get("db")
        organization = context_hub.organization if context_hub else runtime_ctx.get("organization")
        report = context_hub.report if context_hub else runtime_ctx.get("report")

        def _end_error(msg: str) -> ToolEndEvent:
            return ToolEndEvent(
                type="tool.end",
                payload={
                    "output": ReadQueryOutput(success=False, errors=[msg]).model_dump(),
                    "observation": {"summary": f"read_query slice failed: {msg}"},
                },
            )

        if not db or not organization:
            yield _end_error("Missing database or organization context")
            return

        organization_settings = runtime_ctx.get("settings")
        allow_llm_see_data = True
        if organization_settings:
            try:
                allow_llm_see_data = organization_settings.get_config("allow_llm_see_data").value
            except Exception:
                allow_llm_see_data = True

        yield ToolStartEvent(type="tool.start", payload={"mode": "slice"})

        svc = ArtifactStoreService()
        artifact = None
        if data.artifact_id:
            artifact = await svc.get_artifact(db, str(organization.id), data.artifact_id)
            if artifact is None:
                yield _end_error(f"Artifact not found: {data.artifact_id}")
                return
        else:
            # Resolve via query/viz -> step -> latest artifact for that step.
            step_id = None
            query_id = (data.query_ids or [None])[0]
            if not query_id and data.visualization_ids:
                viz_res = await db.execute(
                    select(Visualization).where(Visualization.id == data.visualization_ids[0])
                )
                viz = viz_res.scalar_one_or_none()
                query_id = str(viz.query_id) if viz and viz.query_id else None
            if query_id:
                q_res = await db.execute(select(Query).where(Query.id == str(query_id)))
                q = q_res.scalar_one_or_none()
                if q is not None:
                    step = q.default_step or (q.steps[-1] if q.steps else None)
                    step_id = str(step.id) if step is not None else None
            artifact = await svc.latest_for_step(
                db, str(organization.id), step_id=step_id, query_id=query_id
            )
            if artifact is None:
                yield _end_error(
                    "No artifact found for the given query — the result was small enough to be "
                    "fully stored; read it without slice parameters, or check the artifact_id."
                )
                return

        # Report scoping: an artifact bound to another report is not readable here.
        if report is not None and artifact.report_id and str(artifact.report_id) != str(report.id):
            yield _end_error("Artifact belongs to a different report")
            return

        try:
            result = await svc.slice(
                artifact,
                offset=data.offset or 0,
                limit=data.limit,
                match=data.match,
                match_column=data.match_column,
                columns=data.columns,
                time_from=data.time_from,
                time_to=data.time_to,
                sql=data.sql,
                allow_llm_see_data=allow_llm_see_data,
            )
        except Exception as e:
            yield _end_error(f"{type(e).__name__}: {e}")
            return

        shown = len(result.get("rows", []) or []) if allow_llm_see_data else result.get("returned_rows", 0)
        total = result.get("total_matches")
        summary_bits = [f"Sliced artifact {str(artifact.id)[:8]}: {shown} rows returned"]
        if total is not None:
            summary_bits.append(f"of {total} matching (artifact holds {artifact.row_count} rows total)")
        if result.get("next_offset") is not None:
            summary_bits.append(f"more available at offset {result['next_offset']}")
        summary = "; ".join(summary_bits) + "."

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": ReadQueryOutput(success=True, results=[], slice=result).model_dump(),
                "observation": {
                    "summary": summary,
                    "slice": result,
                    "analysis_complete": False,
                    "final_answer": None,
                },
            },
        )

    async def _resolve_by_viz_id(
        self, db, report, organization, viz_id: str, allow_llm_see_data: bool
    ) -> ReadQueryResult:
        """Resolve a single visualization_id to a ReadQueryResult."""
        try:
            result = await db.execute(
                select(Visualization).where(
                    Visualization.id == viz_id,
                    *([Visualization.report_id == str(report.id)] if report else []),
                )
            )
            visualization = result.scalar_one_or_none()
            if not visualization:
                return ReadQueryResult(visualization_id=viz_id, error=f"Visualization not found: {viz_id}")

            query = None
            if visualization.query_id:
                q_result = await db.execute(
                    select(Query).where(Query.id == visualization.query_id)
                )
                query = q_result.scalar_one_or_none()

            return self._build_result(query, visualization, allow_llm_see_data)
        except Exception as e:
            return ReadQueryResult(visualization_id=viz_id, error=str(e))

    async def _resolve_by_query_id(
        self, db, report, organization, query_id: str, allow_llm_see_data: bool
    ) -> ReadQueryResult:
        """Resolve a single query_id to a ReadQueryResult."""
        try:
            result = await db.execute(
                select(Query).where(
                    Query.id == query_id,
                    Query.organization_id == str(organization.id),
                )
            )
            query = result.scalar_one_or_none()
            if not query:
                return ReadQueryResult(query_id=query_id, error=f"Query not found: {query_id}")

            # Find associated visualization
            viz_result = await db.execute(
                select(Visualization).where(
                    Visualization.query_id == str(query.id),
                ).limit(1)
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
        )

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = ReadQueryInput(**tool_input)

        all_query_ids = data.query_ids or []
        all_viz_ids = data.visualization_ids or []

        # --- Slice mode (Investigation Artifact Store) ---
        slice_params_given = any(
            v is not None
            for v in (data.offset, data.limit, data.match, data.match_column,
                      data.columns, data.time_from, data.time_to, data.sql)
        )
        if data.artifact_id or slice_params_given:
            async for evt in self._run_slice(data, runtime_ctx):
                yield evt
            return

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
            summary_parts.append(f"'{r.title or 'Untitled'}'")
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
            info = r.data.get("info", {}) if r.data and isinstance(r.data, dict) else {}
            observation["stats"] = info if allow_llm_see_data else gate_stats_for_privacy(info)
            if r.data_model:
                observation["data_model"] = r.data_model
            if r.view:
                observation["view"] = r.view
            if r.step_id:
                observation["step_id"] = r.step_id
        elif succeeded:
            # Multiple results: provide a summary of each
            observation["results_summary"] = [
                {
                    "title": r.title,
                    "query_id": r.query_id,
                    "visualization_id": r.visualization_id,
                    "data_model": r.data_model,
                    "data_preview": r.data_preview,
                }
                for r in succeeded
            ]

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output,
                "observation": observation,
            },
        )
