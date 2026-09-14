"""run_query tool — execute an existing query's saved code with parameter VALUES.

The companion to read_query: read_query inspects what a query IS (code,
declared parameters, the stored snapshot), run_query obtains the result FOR
THESE INPUTS. "Revenue by region" created with region=US cannot answer
"what about Germany?" from its snapshot — that needs execution, even though
the query itself does not change.

Runs in VIEWER mode (``QueryService.run_query_viewer``): the saved code
executes with the resolved values, the result is cached per
(step, viewer, values fingerprint), and NO new Step is created — the query's
default step, its visualizations and every other viewer's dashboard are left
exactly as they were. Supplying values is not authoring; changing what the
query returns is create_data's job, and adding a new filter is
add_parameter's.
"""

from typing import Any, AsyncIterator, Dict, List, Optional, Type

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
from app.ai.tools.schemas.run_query import MissingParam, RunQueryInput, RunQueryOutput
from app.models.query import Query
from app.models.step import Step
from app.models.visualization import Visualization
from app.schemas.param_schema import parse_param_specs
from app.schemas.query_schema import QueryRunRequest


def _missing_params(specs, supplied: Dict[str, Any]) -> List[MissingParam]:
    """Declared params with no value from either the caller or a default.

    Identity-sourced params are excluded: they are resolved server-side from
    the viewer and are never the caller's to supply.
    """
    out: List[MissingParam] = []
    for spec in specs:
        if spec.source == "identity":
            continue
        if supplied.get(spec.name) is not None:
            continue
        if spec.default is not None:
            continue
        if not spec.required:
            continue
        out.append(
            MissingParam(
                name=spec.name,
                type=spec.type,
                label=spec.label,
                description=spec.description,
                required=bool(spec.required),
                options=list(spec.options) if spec.options else None,
                options_source=(
                    spec.options_source.model_dump() if spec.options_source else None
                ),
            )
        )
    return out


def _spec_dicts(specs) -> Optional[List[Dict[str, Any]]]:
    return [s.model_dump() for s in specs] or None


class RunQueryTool(Tool):
    """Execute an existing query with supplied parameter values."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="run_query",
            description=(
                "Run an EXISTING query with specific parameter values and return the result. "
                "Use this when the user asks the same question a saved query already answers but "
                "for different inputs — a different region, customer, year, status, time window — "
                "and that input is one of the query's declared parameters (shown as <parameters> on "
                "the query in context, or via read_query). The query's saved code runs with those "
                "values: no code is generated, the query keeps its id and visualizations, and the "
                "saved defaults and the shared dashboard snapshot are NOT changed. "
                "Use create_data instead when the SHAPE of the result must change (different "
                "columns, grouping or aggregation), and add_parameter when the query needs a new "
                "filter it does not yet declare. "
                "IMPORTANT: take query_id from the conversation or report context — do NOT ask the "
                "user for ids. If a required parameter has no value, the result lists it under "
                "missing_params: ask the user for that value rather than guessing."
            ),
            category="research",
            version="1.0.0",
            input_schema=RunQueryInput.model_json_schema(),
            output_schema=RunQueryOutput.model_json_schema(),
            max_retries=0,
            # A warehouse round-trip, not a metadata read: read_query's 30s is a
            # lookup budget. Matches add_parameter, which also executes once.
            timeout_seconds=120,
            # Deterministic for a given value set, and served from the per-viewer
            # cache on repeat — same contract as describe_entity.
            idempotent=True,
            is_active=True,
            required_permissions=[],
            tags=["query", "data", "parameters", "run"],
            observation_policy="on_trigger",
            allowed_modes=["chat", "training"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return RunQueryInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return RunQueryOutput

    def _end(self, output: Dict[str, Any], observation: Dict[str, Any]) -> ToolEndEvent:
        return ToolEndEvent(type="tool.end", payload={"output": output, "observation": observation})

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = RunQueryInput(**tool_input)
        supplied = dict(data.params or {})

        yield ToolStartEvent(
            type="tool.start",
            payload={"query_id": data.query_id, "params": supplied},
        )

        context_hub = runtime_ctx.get("context_hub")
        db = context_hub.db if context_hub else runtime_ctx.get("db")
        organization = context_hub.organization if context_hub else runtime_ctx.get("organization")
        report = context_hub.report if context_hub else runtime_ctx.get("report")
        user = runtime_ctx.get("user")

        if not db or not organization:
            yield self._end(
                RunQueryOutput(
                    success=False, query_id=data.query_id,
                    error="Missing database or organization context",
                ).model_dump(),
                {
                    "summary": "run_query failed: missing context",
                    "error": {"type": "context_error", "message": "Missing db or organization"},
                },
            )
            return

        if user is None:
            yield self._end(
                RunQueryOutput(
                    success=False, query_id=data.query_id,
                    error="run_query requires an authenticated user",
                ).model_dump(),
                {
                    "summary": "run_query failed: no authenticated user",
                    "error": {"type": "context_error", "message": "No user in runtime context"},
                },
            )
            return

        # Scope the lookup to this report, exactly as read_query does — an id
        # from elsewhere in the org is not addressable from this conversation.
        res = await db.execute(
            select(Query)
            .options(lazyload("*"), selectinload(Query.default_step).options(lazyload("*")))
            .where(
                Query.id == str(data.query_id),
                Query.organization_id == str(organization.id),
                *([Query.report_id == str(report.id)] if report is not None else []),
            )
        )
        query = res.scalar_one_or_none()
        if query is None:
            yield self._end(
                RunQueryOutput(
                    success=False, query_id=data.query_id,
                    error=f"Query not found in this report: {data.query_id}",
                ).model_dump(),
                {
                    "summary": f"run_query failed: query {data.query_id} not found in this report.",
                    "error": {
                        "type": "not_found",
                        "message": (
                            "Query not found — pass a query_id from this conversation's "
                            "create_data results or the report context."
                        ),
                    },
                },
            )
            return

        specs = parse_param_specs(getattr(query, "parameters", None))
        declared = _spec_dicts(specs)

        # Nothing to supply values FOR: say so instead of running defaults and
        # letting the planner believe it got a filtered slice.
        if supplied and not specs:
            yield self._end(
                RunQueryOutput(
                    success=False, query_id=str(query.id), title=query.title,
                    error="This query declares no parameters",
                ).model_dump(),
                {
                    "summary": (
                        f"run_query: '{query.title}' declares no parameters, so "
                        f"{', '.join(sorted(supplied))} cannot be applied. Use add_parameter to "
                        f"make it filterable, or create_data for a differently shaped result."
                    ),
                    "error": {
                        "type": "no_parameters",
                        "message": "Query declares no parameters.",
                    },
                },
            )
            return

        unknown = sorted(k for k in supplied if k not in {s.name for s in specs})
        if unknown:
            names = ", ".join(s.name for s in specs)
            yield self._end(
                RunQueryOutput(
                    success=False, query_id=str(query.id), title=query.title,
                    parameters=declared,
                    error=f"Unknown parameter(s): {', '.join(unknown)}",
                ).model_dump(),
                {
                    "summary": (
                        f"run_query: '{query.title}' has no parameter(s) named "
                        f"{', '.join(unknown)}. Declared parameters: {names}."
                    ),
                    "parameters": declared,
                    "error": {
                        "type": "unknown_parameter",
                        "message": f"Unknown: {', '.join(unknown)}. Declared: {names}.",
                    },
                },
            )
            return

        # Required values with nowhere to come from: hand the planner the
        # declarations so it can ASK, rather than an error string to parse.
        missing = _missing_params(specs, supplied)
        if missing:
            yield self._end(
                RunQueryOutput(
                    success=False, query_id=str(query.id), title=query.title,
                    parameters=declared,
                    missing_params=missing,
                ).model_dump(),
                {
                    "summary": (
                        f"run_query needs value(s) for {', '.join(m.name for m in missing)} "
                        f"on '{query.title}'. Ask the user — do not guess."
                    ),
                    "missing_params": [m.model_dump() for m in missing],
                    "parameters": declared,
                    "error": {
                        "type": "missing_parameter",
                        "message": (
                            "Required parameter(s) without a value: "
                            + ", ".join(m.name for m in missing)
                        ),
                    },
                },
            )
            return

        yield ToolProgressEvent(
            type="tool.progress",
            payload={"stage": "executing", "query_id": str(query.id), "params": supplied},
        )

        # Viewer-mode run. `run_as_user_id` is deliberately NOT exposed on this
        # tool's input: view-as is an audited owner/admin flow, and letting the
        # planner pick an identity would read another member's slice.
        request = QueryRunRequest(
            mode="viewer",
            params=supplied or None,
            force_refresh=bool(data.force_refresh),
        )

        from app.ai.code_execution.query_params import ParamError
        from app.services.query_service import QueryService

        try:
            result = await QueryService().run_query_viewer(
                db,
                str(query.id),
                request,
                organization_id=str(organization.id),
                user_id=str(user.id),
            )
        except ParamError as e:
            names = ", ".join(s.name for s in specs)
            yield self._end(
                RunQueryOutput(
                    success=False, query_id=str(query.id), title=query.title,
                    parameters=declared, error=str(e),
                ).model_dump(),
                {
                    "summary": f"run_query rejected the values for '{query.title}': {e}",
                    "parameters": declared,
                    "error": {
                        "type": "param_error",
                        "message": f"{e}" + (f" (declared parameters: {names})" if names else ""),
                    },
                },
            )
            return
        except Exception as e:
            yield self._end(
                RunQueryOutput(
                    success=False, query_id=str(query.id), title=query.title,
                    parameters=declared, error=str(e),
                ).model_dump(),
                {
                    "summary": f"run_query failed to execute '{query.title}': {e}",
                    "error": {"type": "execution_error", "message": str(e)},
                },
            )
            return

        applied = dict(result.get("applied_params") or {})
        params_label = ", ".join(f"{k}={v!r}" for k, v in applied.items()) or "defaults"

        if result.get("status") != "success":
            # Values were requested and the run failed: report the failure. Never
            # fall back to the stored snapshot — it answers a DIFFERENT question,
            # and silently serving it is worse than no rows at all.
            err = result.get("error") or "execution failed"
            yield self._end(
                RunQueryOutput(
                    success=False, query_id=str(query.id), step_id=result.get("step_id"),
                    title=query.title, parameters=declared, applied_params=applied or None,
                    error=err,
                ).model_dump(),
                {
                    "summary": (
                        f"run_query failed for '{query.title}' with {params_label}: {err}. "
                        f"No rows were returned — the stored snapshot answers different values "
                        f"and was NOT substituted."
                    ),
                    "applied_params": applied or None,
                    "error": {"type": "execution_error", "message": err},
                },
            )
            return

        organization_settings = runtime_ctx.get("settings")
        allow_llm_see_data = True
        if organization_settings:
            try:
                allow_llm_see_data = organization_settings.get_config("allow_llm_see_data").value
            except Exception:
                allow_llm_see_data = True

        run_data = result.get("data") or {}
        data_preview = (
            build_data_preview(run_data, allow_llm_see_data=allow_llm_see_data)
            if isinstance(run_data, dict) and run_data
            else None
        )

        step: Optional[Step] = query.default_step
        data_model = step.data_model if step else None
        view = step.view if step else None
        if not view:
            viz = (
                await db.execute(
                    select(Visualization)
                    .options(lazyload("*"))
                    .where(Visualization.query_id == str(query.id))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if viz is not None:
                view = viz.view

        output = RunQueryOutput(
            success=True,
            query_id=str(query.id),
            step_id=result.get("step_id"),
            title=query.title,
            data=run_data,
            data_preview=data_preview,
            data_model=data_model,
            view=view,
            applied_params=applied or None,
            parameters=declared,
            cached=bool(result.get("cached")),
        ).model_dump()

        info = run_data.get("info", {}) if isinstance(run_data, dict) else {}
        observation: Dict[str, Any] = {
            # The values are stated first and always: these rows answer THIS
            # question, not the one the query's stored snapshot answers.
            "summary": (
                f"Ran '{query.title}' with {params_label} — "
                f"{int(info.get('total_rows') or len(run_data.get('rows') or []))} row(s)"
                + (" (from cache)" if result.get("cached") else "")
                + "."
            ),
            "analysis_complete": False,
            "final_answer": None,
            "applied_params": applied or None,
            "data_preview": data_preview,
            "stats": clamp_stats(info) if allow_llm_see_data else clamp_stats(gate_stats_for_privacy(info)),
        }
        if declared:
            observation["parameters"] = declared
        if data_model:
            observation["data_model"] = data_model
        if result.get("step_id"):
            observation["step_id"] = result.get("step_id")

        yield self._end(output, observation)
