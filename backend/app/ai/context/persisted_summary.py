"""Small, durable projections of row-heavy Step and tool payloads.

The full datasets remain the source of truth for the UI and audit history.  The
agent prompt only needs dimensions, references, bounded samples, and aggregate
metadata, so persist those fields beside the large JSON values when the values
are first written.  Prompt construction can then avoid detoasting/parsing tens
of megabytes merely to recover a one-line digest.
"""

from __future__ import annotations

from typing import Any

from app.ai.context.data_preview import (
    MAX_CELL_CHARS,
    clamp_scalar,
    clamp_stats,
)

CONTEXT_SUMMARY_VERSION = 1
STEP_PREVIEW_ROWS = 5


def _column_projection(columns: Any) -> list[dict[str, Any]]:
    """Keep exactly the column identity consumed by context renderers."""
    if not isinstance(columns, list):
        return []
    projected: list[dict[str, Any]] = []
    for column in columns:
        if not isinstance(column, dict):
            continue
        if column.get("field"):
            projected.append({"field": str(column.get("field"))})
        elif column.get("headerName"):
            projected.append({"headerName": str(column.get("headerName"))})
    return projected


def _clamp_row(row: Any) -> Any:
    if isinstance(row, dict):
        return {key: clamp_scalar(value, MAX_CELL_CHARS) for key, value in row.items()}
    if isinstance(row, list):
        return [clamp_scalar(value, MAX_CELL_CHARS) for value in row]
    return clamp_scalar(row, MAX_CELL_CHARS)


def _row_count(data: dict[str, Any], rows: list[Any]) -> int:
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    total = info.get("total_rows")
    if isinstance(total, int) and not isinstance(total, bool):
        return total
    return len(rows)


def build_step_context_summary_from_projection(
    *,
    info: Any,
    columns: Any,
    preview_rows: Any,
    row_count: int | None = None,
) -> dict[str, Any]:
    """Normalize the bounded fields already extracted by the legacy reader."""
    normalized_info = info if isinstance(info, dict) else {}
    normalized_rows = preview_rows if isinstance(preview_rows, list) else []
    if not isinstance(row_count, int) or isinstance(row_count, bool):
        total_rows = normalized_info.get("total_rows")
        row_count = (
            total_rows if isinstance(total_rows, int) and not isinstance(total_rows, bool) else len(normalized_rows)
        )
    return {
        "version": CONTEXT_SUMMARY_VERSION,
        "row_count": row_count,
        "columns": _column_projection(columns),
        "preview_rows": [_clamp_row(row) for row in normalized_rows[:STEP_PREVIEW_ROWS]],
        "info": clamp_stats(normalized_info),
    }


def build_step_context_summary(data: Any) -> dict[str, Any]:
    """Project the current Step snapshot without walking its complete row list."""
    formatted = data if isinstance(data, dict) else {}
    rows = formatted.get("rows") if isinstance(formatted.get("rows"), list) else []
    info = formatted.get("info") if isinstance(formatted.get("info"), dict) else {}
    # QueryContext historically renders the first five rows. Clamp the same
    # cells it clamps at render time, but never copy or scan later rows.
    return build_step_context_summary_from_projection(
        info=info,
        columns=formatted.get("columns"),
        preview_rows=rows[:STEP_PREVIEW_ROWS],
        row_count=_row_count(formatted, rows),
    )


def _preview_shape(
    item: dict[str, Any],
    *,
    include_first_row: bool,
    fallback_to_data: bool = True,
) -> dict[str, Any]:
    preview = item.get("data_preview") if isinstance(item.get("data_preview"), dict) else {}
    if not preview and not fallback_to_data:
        return {}
    data = item.get("data") if fallback_to_data and isinstance(item.get("data"), dict) else {}
    columns = preview.get("columns") or data.get("columns") or []
    preview_rows = preview.get("rows") if isinstance(preview.get("rows"), list) else []
    data_rows = data.get("rows") if isinstance(data.get("rows"), list) else []

    row_count = preview.get("row_count")
    if not isinstance(row_count, int) or isinstance(row_count, bool):
        # create_data's stored preview historically omitted row_count; its
        # digest then fell back to the complete data length. read_query, by
        # contrast, only reasoned from its preview. ``fallback_to_data`` keeps
        # those two existing contracts distinct.
        row_count = len(data_rows) if fallback_to_data and data_rows else len(preview_rows)

    shape: dict[str, Any] = {
        "columns": _column_projection(columns),
        "row_count": row_count,
    }
    if include_first_row:
        first_row = preview_rows[0] if preview_rows else (data_rows[0] if data_rows else None)
        if first_row is not None:
            shape["rows"] = [_clamp_row(first_row)]
    return shape


def build_tool_context_summary(
    tool_name: Any,
    result_json: Any,
) -> dict[str, Any] | None:
    """Return the immutable prompt projection for row-heavy tool results.

    The returned dictionaries intentionally match the lightweight result shapes
    MessageContextBuilder already renders, so switching storage does not change
    the generated context text.
    """
    if not isinstance(result_json, dict):
        return None

    name = str(tool_name or "")
    if name == "create_data":
        preview = _preview_shape(result_json, include_first_row=True)
        stats = result_json.get("stats") if isinstance(result_json.get("stats"), dict) else {}
        total_rows = stats.get("total_rows")
        if not isinstance(total_rows, int) or isinstance(total_rows, bool):
            total_rows = preview.get("row_count", 0)

        projection: dict[str, Any] = {
            "version": CONTEXT_SUMMARY_VERSION,
            "data_preview": preview,
            "stats": {"total_rows": total_rows},
        }
        for field in (
            "success",
            "data_model",
            "view",
            "created_visualization_ids",
            "query_id",
            "query_timings",
            "codegen_ms",
            "execution_ms",
            "errors",
        ):
            if result_json.get(field) is not None:
                projection[field] = result_json.get(field)
        return projection

    if name == "read_query":
        nested = result_json.get("results")
        items = nested if isinstance(nested, list) else [result_json]
        results: list[dict[str, Any]] = []
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            item: dict[str, Any] = {}
            for field in ("query_id", "visualization_id", "title", "error"):
                if raw_item.get(field) is not None:
                    item[field] = raw_item.get(field)
            preview = _preview_shape(
                raw_item,
                include_first_row=False,
                fallback_to_data=False,
            )
            if preview.get("columns") or preview.get("row_count") is not None:
                item["data_preview"] = preview
            if item:
                results.append(item)
        return {
            "version": CONTEXT_SUMMARY_VERSION,
            "success": result_json.get("success"),
            "results": results,
            "errors": result_json.get("errors"),
        }

    return None
