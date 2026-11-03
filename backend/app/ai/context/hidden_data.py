"""Model-facing copies of stored tool results when the model may not see data.

Stored summaries (``ToolExecution.context_summary_json``) are also what the UI
renders, and they may hold values captured while the organization still let
the model see data. When it no longer does ("Allow LLM to see data" off), the
history sent to the model goes through ``hide_data_for_replay``: a copy with
no raw cell values. Structure stays (columns, row counts, references,
parameters, safe aggregate stats), so the model can still reason and reuse.
The stored summary itself is never changed.
"""
from __future__ import annotations

from typing import Any

from app.ai.data_preview import gate_stats_for_privacy
from app.ai.data_visibility import redact_error_text

# Keys whose list/dict values are cells (result rows, profile samples).
_ROW_KEYS = frozenset({"rows", "head_rows", "sample_rows", "sample_values"})
# Per-column stats computed from sample rows (visualization profiles): all of
# it is derived from cells, so it goes as a whole.
_SAMPLE_STATS_KEYS = frozenset({"column_stats"})
_FILE_TOOLS = frozenset({"read_file", "read_email", "read_note"})
# Error text (a DB or conversion error can quote a cell): recorded while data
# was visible, it is cut before the value on replay.
_ERROR_TEXT_KEYS = frozenset({"db_message", "error_message", "error_detail", "last_error_message"})
_ERROR_DICT_TEXT_KEYS = frozenset({"message", "detail", "db_message"})
HIDDEN_NOTE = (
    "Hidden by organization policy (allow_llm_see_data is off): only structure "
    "and aggregate stats are available."
)


def _is_date(dtype: Any) -> bool:
    value = str(dtype or "").lower()
    return "date" in value or "time" in value


def _scrub(value: Any) -> Any:
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    hidden = False
    for key, item in value.items():
        if key in _ROW_KEYS and isinstance(item, (list, dict)):
            hidden = True
            continue
        if key in _SAMPLE_STATS_KEYS and isinstance(item, dict):
            hidden = True
            continue
        if key == "stats" and isinstance(item, dict) and isinstance(item.get("column_info"), dict):
            out[key] = gate_stats_for_privacy(item)
            continue
        if key == "columns" and isinstance(item, list):
            out[key] = [_column(col) for col in item]
            continue
        if key in _ERROR_TEXT_KEYS or (key == "error" and isinstance(item, str)):
            out[key] = redact_error_text(item)
            continue
        if key == "error" and isinstance(item, dict):
            out[key] = {k: redact_error_text(v) if k in _ERROR_DICT_TEXT_KEYS else _scrub(v) for k, v in item.items()}
            continue
        if key == "final_answer" and value.get("terminal_execution_error"):
            # A terminal failure's final answer is the DB error itself.
            out[key] = redact_error_text(item)
            continue
        out[key] = _scrub(item)
    if hidden:
        out["data_hidden"] = True
    return out


def _column(col: Any) -> Any:
    """A column descriptor keeps its min/max only for dates (a time range is
    the one extreme ``gate_stats_for_privacy`` allows)."""
    if not isinstance(col, dict):
        return col
    if ("min" in col or "max" in col) and not _is_date(col.get("dtype")):
        col = {key: item for key, item in col.items() if key not in ("min", "max")}
    return _scrub(col)


def _list_receipts(rows: Any) -> list | None:
    if not isinstance(rows, list):
        return None
    return [{k: v for k, v in row.items() if k != "key"} if isinstance(row, dict) else row for row in rows]


def hide_data_for_replay(body: Any, tool_name: Any = None) -> Any:
    """A copy of ``body`` (a stored tool summary, or the observation envelope
    inside one) without raw cell values. Non-dicts are returned as they are."""
    if not isinstance(body, dict):
        return body
    hidden = _scrub(body)
    name = str(tool_name or "")
    if name == "submit_list":
        # Its "rows" are receipts ({row_id, key, action}), which the agent
        # needs to update or delete those rows later; only the key can be a
        # value.
        receipts = _list_receipts(body.get("rows"))
        if receipts is not None:
            hidden["rows"] = receipts
    if name in _FILE_TOOLS:
        removed = [hidden.pop(field) for field in ("text", "csv") if isinstance(hidden.get(field), str)]
        if removed:
            hidden["data_hidden"] = True
            hidden["note"] = HIDDEN_NOTE
    if name == "write_csv" and isinstance(hidden.get("data_preview"), str):
        hidden.pop("data_preview")
        hidden["data_hidden"] = True
    # inspect_data's details are its printed output (DataFrame heads, values).
    observation = hidden.get("observation") if isinstance(hidden.get("observation"), dict) else None
    for target in (hidden, observation):
        if name == "inspect_data" and target is not None and isinstance(target.get("details"), str):
            target["details"] = HIDDEN_NOTE
            target["data_hidden"] = True
    return hidden
