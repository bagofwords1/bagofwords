"""Bounded database projections for report timeline and summary payloads.

Historical tool executions and Steps can contain multi-megabyte JSON row sets.
Report pages only need a small preview; loading the ORM JSON columns and then
trimming them makes latency and memory scale with data the response discards.
These helpers project legacy rows in SQL and hydrate only bounded UI shapes.
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes

from app.ai.data_preview import DEFAULT_PREVIEW_BUDGET_BYTES, build_data_preview
from app.ai.persisted_summary import (
    CONTEXT_SUMMARY_VERSION,
    SUMMARIZED_TOOL_NAMES,
    build_tool_context_summary,
)
from app.models.step import Step
from app.models.tool_execution import ToolExecution


PROJECTED_TOOL_NAMES = SUMMARIZED_TOOL_NAMES


def _summary_has_ui_fields(summary: Any) -> bool:
    """True when a persisted summary carries the v2 UI projection fields.

    Version-1 summaries (written by pre-upgrade workers) hold only the prompt
    projection — no ui_preview/rows/step_id — and must be rebuilt from the full
    JSON before being served as a card payload.
    """
    return (
        isinstance(summary, dict)
        and summary.get("version") == CONTEXT_SUMMARY_VERSION
    )


def _bounded_step_data(
    *,
    rows: Any,
    columns: Any,
    info: Any,
    row_count: Any,
) -> dict[str, Any]:
    raw_rows = rows if isinstance(rows, list) else []
    raw_columns = columns if isinstance(columns, list) else []
    raw_info = dict(info) if isinstance(info, dict) else {}
    if not isinstance(row_count, int) or isinstance(row_count, bool):
        row_count = raw_info.get("total_rows")
    if not isinstance(row_count, int) or isinstance(row_count, bool):
        row_count = len(raw_rows)
    raw_info.setdefault("total_rows", row_count)
    preview = build_data_preview(
        {"rows": raw_rows, "columns": raw_columns, "info": raw_info},
        budget_bytes=DEFAULT_PREVIEW_BUDGET_BYTES,
    )
    preview_rows = preview.get("rows") if isinstance(preview.get("rows"), list) else []
    data: dict[str, Any] = {
        "rows": preview_rows,
        "columns": raw_columns,
        "info": raw_info,
    }
    if row_count > len(preview_rows) or preview.get("truncated"):
        data["truncated"] = True
        data["total_rows"] = row_count
    if preview.get("cells_truncated"):
        data["cells_truncated"] = True
    if preview.get("note"):
        data["preview_note"] = preview["note"]
    return data


async def hydrate_step_data_for_ui(
    db: AsyncSession,
    steps: Iterable[Step],
    *,
    preview_rows: int,
) -> None:
    """Hydrate small Steps fully and large/legacy Steps as bounded previews."""
    unique = {str(step.id): step for step in steps}
    full_ids: list[str] = []
    legacy_ids: list[str] = []
    for step_id, step in unique.items():
        try:
            if "data" not in sa_inspect(step).unloaded:
                continue
        except Exception:
            continue
        summary = getattr(step, "context_summary_json", None)
        if _summary_has_ui_fields(summary):
            row_count = summary.get("row_count")
            if isinstance(row_count, int) and not isinstance(row_count, bool) and row_count <= preview_rows:
                full_ids.append(step_id)
        else:
            legacy_ids.append(step_id)

    if full_ids:
        rows = await db.execute(select(Step.id, Step.data).where(Step.id.in_(full_ids)))
        for step_id, data in rows.all():
            attributes.set_committed_value(unique[str(step_id)], "data", data)

    if not legacy_ids:
        return

    # The migration populates summaries for historical rows and normal writes
    # populate them synchronously. This is only a defensive fallback for an
    # externally inserted/null row: select each JSON document once, then bound
    # it in Python. Repeated PostgreSQL JSON operators were substantially slower
    # because each field access reparsed the same multi-megabyte value.
    result = await db.execute(select(Step.id, Step.data).where(Step.id.in_(legacy_ids)))
    for step_id, raw in result.all():
        data = raw if isinstance(raw, dict) else {}
        rows = data.get("rows") if isinstance(data.get("rows"), list) else []
        attributes.set_committed_value(
            unique[str(step_id)],
            "data",
            _bounded_step_data(
                rows=rows[:preview_rows],
                columns=data.get("columns"),
                info=data.get("info"),
                row_count=len(rows),
            ),
        )


async def hydrate_tool_results_for_ui(
    db: AsyncSession,
    executions: Iterable[ToolExecution],
    *,
    embedded_step_ids: set[str] | None = None,
) -> None:
    """Populate deferred result_json fields with full or projected results."""
    unique = {str(execution.id): execution for execution in executions if execution is not None}
    embedded_step_ids = embedded_step_ids or set()
    projected_ids: dict[str, list[str]] = {name: [] for name in PROJECTED_TOOL_NAMES}
    full_ids: list[str] = []

    for execution_id, execution in unique.items():
        try:
            if "result_json" not in sa_inspect(execution).unloaded:
                continue
        except Exception:
            continue
        summary = getattr(execution, "context_summary_json", None)
        if execution.tool_name in PROJECTED_TOOL_NAMES and _summary_has_ui_fields(summary):
            attributes.set_committed_value(execution, "result_json", dict(summary))
        elif execution.tool_name in PROJECTED_TOOL_NAMES:
            projected_ids[execution.tool_name].append(execution_id)
        else:
            full_ids.append(execution_id)

    legacy_projected_ids = [
        execution_id
        for tool_name in PROJECTED_TOOL_NAMES
        for execution_id in projected_ids[tool_name]
    ]
    if legacy_projected_ids:
        rows = await db.execute(
            select(
                ToolExecution.id,
                ToolExecution.tool_name,
                ToolExecution.result_json,
            ).where(ToolExecution.id.in_(legacy_projected_ids))
        )
        for execution_id, tool_name, result in rows.all():
            projected = build_tool_context_summary(tool_name, result)
            attributes.set_committed_value(
                unique[str(execution_id)],
                "result_json",
                projected if isinstance(projected, dict) else {},
            )

    if full_ids:
        rows = await db.execute(
            select(ToolExecution.id, ToolExecution.result_json).where(
                ToolExecution.id.in_(full_ids)
            )
        )
        for execution_id, result in rows.all():
            attributes.set_committed_value(unique[str(execution_id)], "result_json", result)

    # When the canonical Step is embedded, do not duplicate its row preview in
    # the tool result. Scalar metadata remains available to the tool card.
    for execution in unique.values():
        step_id = str(execution.created_step_id) if execution.created_step_id else None
        if step_id not in embedded_step_ids or execution.tool_name not in {"create_data", "write_csv"}:
            continue
        result = execution.result_json
        if not isinstance(result, dict):
            continue
        compact = dict(result)
        compact.pop("data", None)
        if execution.tool_name == "write_csv":
            # The linked Step is the canonical preview/config/code source.
            # These fields are duplicate analysis internals and WriteCsvTool
            # does not render them when a Step is present.
            for field in ("data_preview", "stats", "data_model", "view"):
                compact.pop(field, None)
        else:
            preview = compact.get("data_preview")
            if isinstance(preview, dict):
                compact["data_preview"] = {
                    key: value
                    for key, value in preview.items()
                    if key not in {"rows"}
                }
        attributes.set_committed_value(execution, "result_json", compact)
