"""Small, durable projections of row-heavy Step and tool payloads.

The full datasets remain the source of truth for the UI and audit history.  The
agent prompt only needs dimensions, references, bounded samples, and aggregate
metadata, so persist those fields beside the large JSON values when the values
are first written.  Prompt construction can then avoid detoasting/parsing tens
of megabytes merely to recover a one-line digest.
"""

from __future__ import annotations

import json
from typing import Any

from app.ai.data_preview import (
    DEFAULT_PREVIEW_BUDGET_BYTES,
    MAX_CELL_CHARS,
    build_data_preview,
    clamp_scalar,
    clamp_stats,
)

# Version 2 added the ui_preview/rows/step_id projection fields that report
# read endpoints serve directly. Version-1 summaries (written by pre-upgrade
# workers) lack them and must be rebuilt from the full JSON before use.
CONTEXT_SUMMARY_VERSION = 2
STEP_PREVIEW_ROWS = 5
UI_STEP_PREVIEW_ROWS = 20
UI_TOOL_PREVIEW_ROWS = 20
UI_FILE_PREVIEW_CHARS = 4_000
SUMMARIZED_TOOL_NAMES = frozenset(
    {
        "create_data",
        "read_query",
        "write_csv",
        "read_file",
        "read_email",
        "read_note",
    }
)

# Cross-turn observation retention.
#
# The observation is what the model actually SAW when the tool ran. Only the
# row-heavy tools above get a purpose-built projection; every other tool used to
# project nothing, so a later turn could only re-derive an approximation from
# ``result_json`` -- which holds the tool's ``output``, a different payload. That
# re-derivation drifts (an inspect_data observation carries ``summary``; its
# output does not), so the model lost detail it had already paid for.
#
# Persisting the observation keeps the real result replayable across turns and
# across an interrupted run. Bounds below keep one pathological result (e.g. a
# large inlined MCP payload) from dominating a rehydrated transcript.
OBSERVATION_FIELD_MAX_CHARS = 4_000
OBSERVATION_TOTAL_MAX_CHARS = 12_000
# Vision payloads are re-sent as image blocks within their own run and must not
# be replayed from history; the marker keeps the fact of the image legible.
OBSERVATION_DROPPED_KEYS = ("images", "images_provided_as_vision")


def _clamp_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\u2026 [truncated, {len(value)} chars total]"


def bounded_observation(observation: Any) -> dict[str, Any] | None:
    """Bound one observation for durable storage.

    Copies rather than mutates: the live observation is shared by reference with
    ObservationContextBuilder, which compacts entries in place as the run
    proceeds. Building a new dict here snapshots the value at full fidelity
    before that happens.
    """
    if not isinstance(observation, dict) or not observation:
        return None

    bounded: dict[str, Any] = {}
    for key, value in observation.items():
        if key in OBSERVATION_DROPPED_KEYS:
            continue
        if isinstance(value, str):
            bounded[key] = _clamp_text(value, OBSERVATION_FIELD_MAX_CHARS)
        else:
            bounded[key] = value

    if any(observation.get(key) for key in OBSERVATION_DROPPED_KEYS):
        bounded["images_elided"] = (
            "image(s) were produced and shown when this call ran - "
            "re-view with read_file if needed"
        )

    # Whole-observation ceiling. Serializing is the only honest measure of what
    # a nested structure costs; drop the largest remaining fields until it fits
    # rather than truncating mid-JSON.
    try:
        while len(json.dumps(bounded, default=str)) > OBSERVATION_TOTAL_MAX_CHARS:
            droppable = [
                k for k in bounded
                if k not in ("summary", "success", "error", "images_elided")
            ]
            if not droppable:
                break
            widest = max(
                droppable,
                key=lambda k: len(json.dumps(bounded[k], default=str)),
            )
            size = len(json.dumps(bounded[widest], default=str))
            del bounded[widest]
            bounded[f"{widest}_elided"] = f"{size} chars elided from history"
    except Exception:
        pass

    return bounded or None


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
    include_ui_preview: bool = True,
) -> dict[str, Any]:
    """Normalize the bounded fields already extracted by the legacy reader."""
    normalized_info = info if isinstance(info, dict) else {}
    normalized_rows = preview_rows if isinstance(preview_rows, list) else []
    if not isinstance(row_count, int) or isinstance(row_count, bool):
        total_rows = normalized_info.get("total_rows")
        row_count = (
            total_rows if isinstance(total_rows, int) and not isinstance(total_rows, bool) else len(normalized_rows)
        )
    summary = {
        "version": CONTEXT_SUMMARY_VERSION,
        "row_count": row_count,
        "columns": _column_projection(columns),
        "preview_rows": [_clamp_row(row) for row in normalized_rows[:STEP_PREVIEW_ROWS]],
        "info": clamp_stats(normalized_info),
    }
    # Legacy prompt projection paths only have five rows available. Keeping
    # those rows here still gives report readers a bounded fallback; callers
    # that go on to build the full 20-row UI preview themselves skip this pass.
    if include_ui_preview:
        summary["ui_preview"] = _build_step_ui_preview(
            rows=normalized_rows,
            columns=columns,
            info=normalized_info,
            row_count=row_count,
        )
    return summary


def _build_step_ui_preview(
    *,
    rows: Any,
    columns: Any,
    info: Any,
    row_count: int,
) -> dict[str, Any]:
    """Build the existing report-card preview without scanning later rows."""
    raw_rows = rows if isinstance(rows, list) else []
    raw_columns = columns if isinstance(columns, list) else []
    raw_info = info if isinstance(info, dict) else {}
    preview = build_data_preview(
        {
            "rows": raw_rows[:UI_STEP_PREVIEW_ROWS],
            "columns": raw_columns,
            "info": raw_info,
        },
        budget_bytes=DEFAULT_PREVIEW_BUDGET_BYTES,
    )
    preview_rows = preview.get("rows") if isinstance(preview.get("rows"), list) else []
    result: dict[str, Any] = {
        "rows": preview_rows,
        "columns": preview.get("columns") if isinstance(preview.get("columns"), list) else raw_columns,
        "info": preview.get("info") if isinstance(preview.get("info"), dict) else raw_info,
    }
    if row_count > len(preview_rows) or preview.get("truncated"):
        result["truncated"] = True
        result["total_rows"] = row_count
    if preview.get("cells_truncated"):
        result["cells_truncated"] = True
    if preview.get("note"):
        result["preview_note"] = preview["note"]
    return result


def build_step_context_summary(data: Any) -> dict[str, Any]:
    """Project the current Step snapshot without walking its complete row list."""
    formatted = data if isinstance(data, dict) else {}
    rows = formatted.get("rows") if isinstance(formatted.get("rows"), list) else []
    info = formatted.get("info") if isinstance(formatted.get("info"), dict) else {}
    # QueryContext historically renders the first five rows. Clamp the same
    # cells it clamps at render time, but never copy or scan later rows.
    summary = build_step_context_summary_from_projection(
        info=info,
        columns=formatted.get("columns"),
        preview_rows=rows[:STEP_PREVIEW_ROWS],
        row_count=_row_count(formatted, rows),
        include_ui_preview=False,
    )
    summary["ui_preview"] = _build_step_ui_preview(
        rows=rows,
        columns=formatted.get("columns"),
        info=info,
        row_count=summary["row_count"],
    )
    return summary


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


def _tool_ui_preview(item: dict[str, Any]) -> dict[str, Any]:
    """Keep the bounded rows needed to paint a historical tool card."""
    preview = item.get("data_preview") if isinstance(item.get("data_preview"), dict) else {}
    rows = preview.get("rows") if isinstance(preview.get("rows"), list) else []
    row_count = preview.get("row_count")
    if not isinstance(row_count, int) or isinstance(row_count, bool):
        row_count = len(rows)
    result: dict[str, Any] = {
        "columns": _column_projection(preview.get("columns")),
        "rows": [_clamp_row(row) for row in rows[:UI_TOOL_PREVIEW_ROWS]],
        "row_count": row_count,
    }
    for field in ("truncated", "cells_truncated", "data_hidden", "note"):
        if preview.get(field) is not None:
            result[field] = preview.get(field)
    if row_count > len(result["rows"]):
        result["truncated"] = True
    return result


def build_tool_context_summary(
    tool_name: Any,
    result_json: Any,
    observation: Any = None,
) -> dict[str, Any] | None:
    """Return the immutable prompt projection for a tool result.

    The returned dictionaries intentionally match the lightweight result shapes
    MessageContextBuilder already renders, so switching storage does not change
    the generated context text.

    Row-heavy tools keep their purpose-built projections (report cards read
    them, and their previews are already bounded). Every other tool projects its
    ``observation`` instead, so later turns replay what the model actually saw.
    """
    name = str(tool_name or "")

    if not isinstance(result_json, dict):
        # An observation-only tool stores no result_json at all. Its observation
        # is still the whole story, so project that rather than nothing.
        bounded = bounded_observation(observation)
        if bounded is None:
            return None
        return {"version": CONTEXT_SUMMARY_VERSION, "observation": bounded}
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
            for field in (
                "query_id",
                "visualization_id",
                "step_id",
                "title",
                "error",
                "data_model",
                "view",
            ):
                if raw_item.get(field) is not None:
                    item[field] = raw_item.get(field)
            preview = _tool_ui_preview(raw_item)
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

    if name == "write_csv":
        preview = _preview_shape(result_json, include_first_row=True)
        projection = {
            "version": CONTEXT_SUMMARY_VERSION,
            "success": result_json.get("success"),
            "data_preview": preview,
        }
        for field in (
            "file_id",
            "file_name",
            "row_count",
            "columns",
            "error_message",
            "stats",
            "data_model",
            "view",
            "step_id",
        ):
            if result_json.get(field) is not None:
                projection[field] = result_json.get(field)
        return projection

    if name in {"read_file", "read_email", "read_note"}:
        # ReadFileTool already renders at most 4,000 characters. Persist that
        # exact visible excerpt plus the scalar badges/identifiers, never the
        # complete historical file body or binary material.
        projection = {"version": CONTEXT_SUMMARY_VERSION}
        for field in (
            "success",
            "connection_id",
            "file_id",
            "file_name",
            "path",
            "content_type",
            "row_count",
            "rows_shown",
            "col_count",
            "truncated",
            "byte_count",
            "garbled",
            "session_file_id",
            "windowed",
            "next_cursor",
            "total_size",
            "eof",
            "encoding",
            "image_count",
            "image_file_ids",
            "pages_total",
            "pages_shown",
            "error",
        ):
            if result_json.get(field) is not None:
                projection[field] = result_json.get(field)
        for field in ("csv", "text"):
            value = result_json.get(field)
            if isinstance(value, str):
                projection[field] = value[:UI_FILE_PREVIEW_CHARS]
        return projection

    # Every remaining tool: keep the observation itself. This is the path that
    # gives cross-turn memory to inspect_data, execute_mcp, the artifact tools
    # and the rest -- previously they projected nothing and history had to
    # re-derive a digest from a payload that was never the observation.
    bounded = bounded_observation(observation)
    if bounded is None:
        return None
    return {"version": CONTEXT_SUMMARY_VERSION, "observation": bounded}
