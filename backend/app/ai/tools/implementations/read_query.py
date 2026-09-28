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


PAGE_DEFAULT_LIMIT = 100
PAGE_MAX_LIMIT = 500
# Same byte budget as the create_data / read_query preview: a page never puts
# more row data in front of the model than a preview would.
PAGE_BUDGET_BYTES = 48_000
PAGE_MAX_CELL_CHARS = 1_000


def _clip_cell(v: Any) -> Any:
    if isinstance(v, str) and len(v) > PAGE_MAX_CELL_CHARS:
        return v[:PAGE_MAX_CELL_CHARS] + "…"
    return v


def build_page(rows: List[Dict[str, Any]], columns: List[str], *, offset: int, limit: int,
               total_rows: int, source: str, show_rows: bool = True) -> Dict[str, Any]:
    """One window of rows, trimmed to the byte budget. ``rows`` is already the
    window (row ``offset`` first); ``next_offset`` accounts for any trimming so
    paging never skips rows."""
    import json as _json

    kept: List[Dict[str, Any]] = []
    used = 0
    if show_rows:
        for r in rows[:limit]:
            clipped = {k: _clip_cell(v) for k, v in r.items()}
            size = len(_json.dumps(clipped, default=str))
            if kept and used + size > PAGE_BUDGET_BYTES:
                break
            kept.append(clipped)
            used += size
    returned = len(kept) if show_rows else min(limit, max(0, total_rows - offset))
    nxt = offset + returned
    eof = nxt >= total_rows
    page: Dict[str, Any] = {
        "offset": offset, "limit": limit, "returned": returned, "total_rows": total_rows,
        "next_offset": None if eof else nxt, "eof": eof, "source": source, "columns": columns,
    }
    if show_rows:
        page["rows"] = kept
    else:
        page["note"] = "Row values are hidden from the model by org policy (allow_llm_see_data is off)."
    return page


def _snapshot_columns(step_data: Dict[str, Any]) -> List[str]:
    cols = [c.get("field") or c.get("headerName") for c in (step_data.get("columns") or []) if isinstance(c, dict)]
    if not cols and step_data.get("rows"):
        cols = list(step_data["rows"][0].keys())
    return [c for c in cols if c]


def _df_window(df, offset: int, limit: int) -> List[Dict[str, Any]]:
    import json as _json
    window = df.iloc[offset: offset + limit]
    return _json.loads(window.to_json(orient="records", date_format="iso", default_handler=str))


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
                "To read rows beyond the preview (or all of a large result), page with offset/limit and follow page.next_offset until page.eof. "
                "IMPORTANT: Extract the query_id or viz_id from previous tool results in the conversation — do NOT ask the user for IDs."
            ),
            category="research",
            version="1.0.0",
            input_schema=ReadQueryInput.model_json_schema(),
            output_schema=ReadQueryOutput.model_json_schema(),
            max_retries=0,
            # Paging past the saved snapshot re-runs the query.
            timeout_seconds=120,
            idempotent=True,
            is_active=True,
            required_permissions=[],
            tags=["query", "visualization", "data", "read"],
            observation_policy="on_trigger",
            allowed_modes=["chat", "training"],
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

    async def _with_page(self, db, runtime_ctx, report, organization, r: ReadQueryResult,
                         data: ReadQueryInput, allow_llm_see_data: bool) -> ReadQueryResult:
        """Attach the requested rows window to ``r``.

        Served from the saved snapshot when the window lies inside it (or the
        snapshot is the whole result). Past the snapshot — which the org row
        limit caps — the step's code is re-run (nothing is persisted), but only
        for queries in THIS report, using this run's own authorized clients.
        """
        offset = data.offset or 0
        limit = data.limit or PAGE_DEFAULT_LIMIT
        settings = runtime_ctx.get("settings")
        try:
            cap = settings.get_config("limit_row_count").value if settings else None
            if isinstance(cap, (int, float)) and cap > 0:
                limit = min(limit, int(cap))
        except Exception:
            pass
        limit = max(1, min(limit, PAGE_MAX_LIMIT))

        step_data = r.data if isinstance(r.data, dict) else {}
        snap_rows = step_data.get("rows") or []
        info = step_data.get("info") or {}
        total = int(info.get("total_rows") or len(snap_rows))
        columns = _snapshot_columns(step_data)

        if offset + limit <= len(snap_rows) or len(snap_rows) >= total or offset >= total:
            r.page = build_page(snap_rows[offset: offset + limit], columns, offset=offset, limit=limit,
                                total_rows=total, source="snapshot", show_rows=allow_llm_see_data)
            return r

        query_report_id = await db.scalar(select(Query.report_id).where(Query.id == r.query_id)) if r.query_id else None
        if not r.step_id or report is None or str(query_report_id) != str(report.id):
            page = build_page(snap_rows[offset: offset + limit], columns, offset=offset, limit=limit,
                              total_rows=min(total, len(snap_rows)), source="snapshot",
                              show_rows=allow_llm_see_data)
            page["note"] = ("Only the saved snapshot of this query can be paged here "
                            f"({len(snap_rows)} of {total} rows); re-run it in this report to read further.")
            r.page = page
            return r

        from app.models.report import Report
        from app.services.step_service import StepService

        svc = StepService()
        try:
            full_report = (await db.execute(
                select(Report).options(selectinload(Report.data_sources), selectinload(Report.files))
                .where(Report.id == str(report.id))
            )).scalar_one()
            step, _ = await svc._load_step_for_rerun(db, r.step_id, report=full_report)
            specs = svc._step_param_specs(step)
            user = runtime_ctx.get("user")
            params = await svc._resolve_step_params(
                db, step, getattr(step, "applied_params", None), user, str(organization.id), specs)
            df = await svc._execute_step_code(
                db, step, full_report, current_user=user,
                db_clients=runtime_ctx.get("ds_clients") or None,
                organization=organization, organization_settings=settings,
                params=params, param_specs=specs, return_raw_df=True,
            )
        except Exception as exc:
            r.error = f"Could not re-run the query to read rows past the snapshot: {exc}"
            return r
        total = int(len(df))
        cols = [str(c) for c in df.columns]
        rows = _df_window(df, offset, limit) if allow_llm_see_data and offset < total else []
        r.page = build_page(rows, cols, offset=offset, limit=limit, total_rows=total,
                            source="re-executed", show_rows=allow_llm_see_data)
        return r

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

        # Saved monitoring data is gated before any rows, code, or titles reach the model.
        from app.services.bow_source_access import can_read, report_access, protect_report
        checked = []
        for r in results:
            rid = await db.scalar(select(Query.report_id).where(Query.id == r.query_id)) if r.query_id else None
            access = await report_access(db, rid)
            if not await can_read(db, access, runtime_ctx.get("user")):
                checked.append(ReadQueryResult(query_id=r.query_id, error="Access denied"))
                continue
            if access:
                await protect_report(db, getattr(report, "id", None), access)
            checked.append(r)
        results = checked

        if data.offset is not None or data.limit is not None:
            results = [await self._with_page(db, runtime_ctx, report, organization, r, data, allow_llm_see_data)
                       if not r.error else r for r in results]

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
            if r.page is not None:
                # Paging: the requested window replaces the preview.
                observation["page"] = r.page
                observation["summary"] = summary + (
                    f" Rows {r.page['offset']}–{r.page['offset'] + r.page['returned'] - 1} of {r.page['total_rows']}"
                    + (" (end)." if r.page["eof"] else f"; continue with offset={r.page['next_offset']}.")
                    if r.page["returned"] else f" No rows at offset {r.page['offset']} (total {r.page['total_rows']})."
                )
            else:
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
                    "data_preview": r.data_preview if r.page is None else None,
                }
                if r.page is not None:
                    entry["page"] = r.page
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
