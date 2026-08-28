"""add_parameter tool — retro-parameterization of an existing query.

Adds ONE declared parameter to an EXISTING query without rebuilding it: a
focused codegen call rewrites only the filtering predicate (binding the
parameter to the given column with the safe ``:name`` placeholder contract),
then the standard builder-run path (``QueryService.run_query_new_step``)
validates declarations-vs-code, executes once with defaults, persists the new
step, and promotes it to the query's default. Visualizations bound to the
query pick the parameter up automatically; the dashboard still needs its
control wired via useParams() (edit_artifact next).

This removes the "regenerate the whole query to add a filter" tax — see
docs/design/artifact-iteration-and-filtering.md (F3).
"""

import logging
import re
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel
from sqlalchemy import select

from app.ai.llm import LLM
from app.ai.llm.types import Message, TextDeltaEvent
from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from app.ai.tools.schemas.add_parameter import AddParameterInput, AddParameterOutput
from app.dependencies import async_session_maker
from app.models.query import Query
from app.models.step import Step
from app.schemas.query_schema import QueryRunRequest

logger = logging.getLogger(__name__)

_CODE_BLOCK_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)

_REWRITE_PROMPT = """You are modifying an existing data-query Python script. Rewrite it to add ONE new parameter — change NOTHING else.

CURRENT CODE:
```python
{code}
```

NEW PARAMETER: name=`{name}`, type={ptype}, required={required}, column to filter: `{column}`

PARAMETERS CONTRACT (MANDATORY):
- `generate_df` must accept a `params` argument: `def generate_df(ds_clients, excel_files, params)` (add it if missing; keep existing extra args like load_step/load_entity in place).
- Put a `:{name}` placeholder in the SQL and pass the value via `execute_query(sql, params={{..., '{name}': params['{name}']}})` — include every placeholder used in that SQL. NEVER f-string, concatenate, or .format() a param value into SQL.
- Optional parameter (required=false): None means 'all' — use `AND (:{name} IS NULL OR {column} = :{name})`.
- List-typed parameter: use `{column} IN :{name}` (and `(:{name} IS NULL OR {column} IN :{name})` when optional).
- Keep every existing parameter, predicate, column, join, ordering, and all non-SQL logic byte-identical. The ONLY changes allowed: the new predicate, the new params entry, and the `params` argument if it was missing.
{error_context}
Output ONLY the full corrected Python code in a single ```python code block. No explanations."""


class AddParameterTool(Tool):
    """Add a declared parameter to an existing query in place."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="add_parameter",
            description=(
                "Add ONE declared parameter (filter) to an EXISTING query WITHOUT rebuilding it. "
                "Use this instead of create_data when the query already returns the right data and "
                "only needs a new viewer-adjustable filter bound to a column it already selects from. "
                "The query keeps its id and visualizations; the platform rewrites the predicate with "
                "the safe :name placeholder contract, re-runs once, and promotes the new step. "
                "For enum-like dimensions pass options_source referencing a dimension query (filter-space "
                "pattern). After adding, wire the dashboard control via useParams() with edit_artifact. "
                "NOT for changing what the query returns — use create_data for reshaping."
            ),
            category="action",
            version="1.0.0",
            input_schema=AddParameterInput.model_json_schema(),
            output_schema=AddParameterOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=120,
            idempotent=False,
            required_permissions=[],
            is_active=True,
            tags=["data", "parameters", "filters"],
            allowed_modes=["chat"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return AddParameterInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return AddParameterOutput

    async def _rewrite_code(self, code: str, data: AddParameterInput, runtime_ctx: Dict[str, Any], error_context: str = "") -> str:
        prompt = _REWRITE_PROMPT.format(
            code=code,
            name=data.parameter.name,
            ptype=data.parameter.type,
            required=str(bool(data.parameter.required)).lower(),
            column=data.column,
            error_context=(f"\nPREVIOUS ATTEMPT FAILED WITH: {error_context}\nFix exactly that.\n" if error_context else ""),
        )
        llm = LLM(runtime_ctx.get("model"), usage_session_maker=async_session_maker)
        report = runtime_ctx.get("report")
        buffer = ""
        async for evt in llm.inference_stream_v2(
            messages=[Message(role="user", content=prompt)],
            usage_scope="add_parameter",
            usage_scope_ref_id=str(report.id) if report else None,
        ):
            if isinstance(evt, TextDeltaEvent):
                buffer += evt.text
        m = _CODE_BLOCK_RE.search(buffer)
        return (m.group(1) if m else buffer).strip()

    def _end(self, output: Dict[str, Any], observation: Dict[str, Any]) -> ToolEndEvent:
        return ToolEndEvent(type="tool.end", payload={"output": output, "observation": observation})

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = AddParameterInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": f"Add parameter '{data.parameter.name}'"})

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        user = runtime_ctx.get("user")
        organization = runtime_ctx.get("organization")

        res = await db.execute(select(Query).where(Query.id == str(data.query_id)))
        q = res.scalar_one_or_none()
        if q is None or (report is not None and str(q.report_id) != str(report.id)):
            yield self._end(
                {"success": False, "query_id": data.query_id, "error": "Query not found in this report"},
                {"summary": f"add_parameter failed: query {data.query_id} not found in this report.",
                 "error": {"type": "not_found", "message": "Query not found in this report — pass a query_id from this conversation's create_data results."}},
            )
            return

        existing = list(getattr(q, "parameters", None) or [])
        if any((p or {}).get("name") == data.parameter.name for p in existing):
            yield self._end(
                {"success": False, "query_id": str(q.id), "error": f"Parameter '{data.parameter.name}' already exists"},
                {"summary": f"add_parameter: '{data.parameter.name}' is already declared on query '{q.title}'. No change made.",
                 "error": {"type": "duplicate_parameter", "message": "Parameter already declared — wire its control in the artifact instead."}},
            )
            return

        step = await db.get(Step, str(q.default_step_id)) if getattr(q, "default_step_id", None) else None
        code = getattr(step, "code", None) or ""
        if not code.strip():
            yield self._end(
                {"success": False, "query_id": str(q.id), "error": "Query has no default step code"},
                {"summary": f"add_parameter failed: query '{q.title}' has no saved code to parameterize.",
                 "error": {"type": "no_code", "message": "Recreate the query via create_data declaring the parameter instead."}},
            )
            return

        merged = existing + [data.parameter.model_dump()]
        from app.services.query_service import QueryService
        service = QueryService()
        org_id = str(organization.id) if organization else None
        user_id = str(user.id) if user else None

        async def _rollback_declaration() -> None:
            """run_query_new_step persists the merged declaration BEFORE
            executing; a failed run must not leave a declared-but-dead param
            (a control that renders but filters nothing)."""
            try:
                await db.refresh(q)
                q.parameters = existing
                db.add(q)
                await db.commit()
            except Exception:
                logger.warning("add_parameter: failed to roll back parameter declaration", exc_info=True)

        last_error = ""
        for attempt in (1, 2):
            yield ToolProgressEvent(type="tool.progress", payload={"stage": "rewriting_code", "attempt": attempt})
            try:
                new_code = await self._rewrite_code(code, data, runtime_ctx, error_context=last_error)
            except Exception as e:
                last_error = f"codegen failed: {e}"
                continue
            if not new_code or f":{data.parameter.name}" not in new_code:
                last_error = f"rewritten code does not use the :{data.parameter.name} placeholder"
                continue
            yield ToolProgressEvent(type="tool.progress", payload={"stage": "executing", "attempt": attempt})
            try:
                q_schema, step_schema = await service.run_query_new_step(
                    db,
                    str(q.id),
                    QueryRunRequest(code=new_code, parameters=merged, params={}, title=q.title),
                    organization_id=org_id,
                    user_id=user_id,
                )
            except Exception as e:
                last_error = str(e)
                await _rollback_declaration()
                continue
            if (step_schema or {}).get("status") != "success":
                last_error = (step_schema or {}).get("status_reason") or "execution failed"
                await _rollback_declaration()
                continue

            rows = ((step_schema.get("data") or {}).get("rows")) or []
            row_count = ((step_schema.get("data") or {}).get("info") or {}).get("total_rows") or len(rows)
            param_names = [str((p or {}).get("name")) for p in (q_schema.get("parameters") or [])]
            yield self._end(
                {
                    "success": True,
                    "query_id": str(q.id),
                    "title": q_schema.get("title"),
                    "parameters": q_schema.get("parameters"),
                    "step_id": step_schema.get("id"),
                    "row_count": row_count,
                },
                {
                    "summary": (
                        f"Added parameter '{data.parameter.name}' to query '{q_schema.get('title')}' in place "
                        f"(now declares: {', '.join(param_names)}). Re-ran with defaults: {row_count} rows. "
                        "The query and its visualizations keep their ids. NEXT: wire a control for "
                        f"'{data.parameter.name}' in the artifact via useParams() (edit_artifact) if a dashboard shows this query."
                    ),
                    "query_id": str(q.id),
                    "parameters": q_schema.get("parameters"),
                    "step_id": step_schema.get("id"),
                },
            )
            return

        yield self._end(
            {"success": False, "query_id": str(q.id), "error": last_error or "failed"},
            {"summary": f"add_parameter failed for query '{q.title}' after 2 attempts: {last_error}",
             "error": {"type": "rewrite_failed", "message": last_error or "failed",
                       "remediation": "Fall back to create_data: recreate this query declaring the parameter (filter-space pattern), then swap the viz id in the artifact."}},
        )
