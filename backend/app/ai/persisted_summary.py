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
GENERIC_TOOL_CONTEXT_BUDGET_BYTES = 16_384
GENERIC_TOOL_CONTEXT_MAX_STRING_CHARS = 2_000
GENERIC_TOOL_CONTEXT_MAX_LIST_ITEMS = 20
TOOL_CONTEXT_KIND_FIELD = "_context_kind"
TOOL_CONTEXT_OBSERVATION_KIND = "model_observation_v1"
_TOOL_CONTEXT_ENVELOPE_OVERHEAD_BYTES = 256
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

_GENERIC_PRIORITY_KEYS = (
    "success",
    "summary",
    "observation",
    "error",
    "errors",
    "result",
    "output",
    "instruction_id",
    "build_id",
    "step_id",
    "query_id",
    "artifact_id",
    "visualization_id",
    "created_visualization_ids",
    "visualization_ids",
    "file_id",
    "session_file_id",
    "title",
    "row_count",
    "columns",
)
_GENERIC_MEDIA_KEYS = frozenset(
    {
        "image",
        "images",
        "image_data",
        "base64",
        "bytes",
        "binary",
        "pdf_bytes",
        "audio_data",
        "images_provided_as_vision",
    }
)


def _generic_tool_projection(
    value: dict[str, Any],
    *,
    include_version: bool = True,
    budget_bytes: int = GENERIC_TOOL_CONTEXT_BUDGET_BYTES,
) -> dict[str, Any]:
    """Bound an ordinary tool result without an LLM call or raw-media copy.

    Small results survive almost unchanged. Large/nested results keep the
    semantically useful head, ids and errors under one hard byte budget. The
    traversal is linear only in the retained prefix, so this remains a tiny
    write-time cost even when ``result_json`` contains a very large row list.
    """

    remaining = [budget_bytes - 512]
    truncated = [False]

    def _consume(size: int) -> bool:
        if remaining[0] <= 0:
            truncated[0] = True
            return False
        remaining[0] -= max(size, 0)
        if remaining[0] < 0:
            truncated[0] = True
            return False
        return True

    def _project(item: Any, *, key: str = "", depth: int = 0) -> Any:
        if depth > 6:
            truncated[0] = True
            return "[nested value elided]"
        if key.lower() in _GENERIC_MEDIA_KEYS and item is not None:
            truncated[0] = True
            marker = "[media elided]"
            _consume(len(marker))
            return marker
        if item is None or isinstance(item, (bool, int, float)):
            _consume(len(str(item)))
            return item
        if isinstance(item, str):
            allowed = min(GENERIC_TOOL_CONTEXT_MAX_STRING_CHARS, max(remaining[0], 0))
            if len(item) > allowed:
                truncated[0] = True
                text = item[:allowed] + "…"
            else:
                text = item
            _consume(len(text.encode("utf-8", errors="ignore")))
            return text
        if isinstance(item, dict):
            out: dict[str, Any] = {}
            keys = list(item)
            ordered = [k for k in _GENERIC_PRIORITY_KEYS if k in item]
            ordered.extend(k for k in keys if k not in ordered)
            for raw_key in ordered:
                child_key = str(raw_key)
                if not _consume(len(child_key) + 4):
                    break
                out[child_key] = _project(
                    item[raw_key], key=child_key, depth=depth + 1
                )
                if remaining[0] <= 0:
                    break
            if len(out) < len(item):
                truncated[0] = True
            return out
        if isinstance(item, (list, tuple)):
            limit = 5 if key.lower() in {"rows", "data"} else GENERIC_TOOL_CONTEXT_MAX_LIST_ITEMS
            out = []
            for child in item[:limit]:
                if remaining[0] <= 0:
                    break
                out.append(_project(child, key=key, depth=depth + 1))
            if len(out) < len(item):
                truncated[0] = True
            return out
        text = str(item)
        return _project(text, key=key, depth=depth)

    projected = _project(value)
    if not isinstance(projected, dict):
        projected = {"value": projected}
    if include_version:
        projected["version"] = CONTEXT_SUMMARY_VERSION
    if truncated[0]:
        projected["_context_truncated"] = True

    # The running budget is deliberately conservative, but multibyte strings
    # can still make encoded JSON larger than their character count. Trim only
    # non-priority tail keys as a final deterministic guard.
    while len(json.dumps(projected, default=str).encode("utf-8")) > budget_bytes:
        removable = [
            key for key in projected
            if key not in _GENERIC_PRIORITY_KEYS
            and key not in (
                {"version", "_context_truncated"}
                if include_version
                else {"_context_truncated"}
            )
        ]
        if not removable:
            # Priority content alone is oversized. Keep an explicit marker;
            # individual strings were already clamped above.
            projected = {
                "summary": str(value.get("summary") or "")[:GENERIC_TOOL_CONTEXT_MAX_STRING_CHARS],
                "_context_truncated": True,
            }
            if include_version:
                projected["version"] = CONTEXT_SUMMARY_VERSION
            break
        projected.pop(removable[-1], None)
        projected["_context_truncated"] = True
    return projected


def tool_context_for_replay(summary: Any) -> Any:
    """Return the model-visible body from a persisted context projection."""
    if (
        isinstance(summary, dict)
        and summary.get(TOOL_CONTEXT_KIND_FIELD) == TOOL_CONTEXT_OBSERVATION_KIND
        and isinstance(summary.get("observation"), dict)
    ):
        return summary["observation"]
    return summary


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
    """Return the immutable, bounded prompt projection for a tool result.

    Row-heavy tools retain their purpose-built shapes. Every other tool keeps
    the bounded observation shown to the model, falling back to a generic
    result projection for historical and non-agent callers that lack one.
    """
    name = str(tool_name or "")
    if name in SUMMARIZED_TOOL_NAMES and not isinstance(result_json, dict):
        return None
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

    # Ordinary tools replay what the model actually saw. Keep it in a tagged
    # envelope so an observation's own fields (including "version") cannot
    # collide with projection metadata. The canonical result remains in
    # result_json for UI/audit consumers.
    if isinstance(observation, dict) and observation:
        return {
            "version": CONTEXT_SUMMARY_VERSION,
            TOOL_CONTEXT_KIND_FIELD: TOOL_CONTEXT_OBSERVATION_KIND,
            "observation": _generic_tool_projection(
                observation,
                include_version=False,
                budget_bytes=(
                    GENERIC_TOOL_CONTEXT_BUDGET_BYTES
                    - _TOOL_CONTEXT_ENVELOPE_OVERHEAD_BYTES
                ),
            ),
        }

    if not isinstance(result_json, dict):
        return None
    return _generic_tool_projection(result_json)
