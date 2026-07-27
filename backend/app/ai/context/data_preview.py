"""Budgeted, self-describing data previews for tool observations.

Single source of truth for how create_data / read_query results are rendered
into the LLM prompt. Replaces scattered ``rows[:5]`` slices.

The *latest* observation carries as many rows as fit a byte budget (so small
results — the common case — come through whole), mirroring how ``read_artifact``
keeps full code for one iteration. Larger results degrade to head+tail with an
explicit truncation note. Older observations are sampled down separately by the
observation compaction layer.
"""
import json
from typing import Any, Dict, List

# Byte budget for a single latest-observation preview (~12k tokens). Comfortably
# read_artifact-scale and well under Snowflake's 250 KB per-response ceiling.
DEFAULT_PREVIEW_BUDGET_BYTES = 48_000

# Rows kept when an older observation is compacted to a sample.
SAMPLE_ROWS = 3

# Max characters kept for any single cell value. The row budget above bounds how
# many rows are shown, but nothing bounded how *wide* one value could be: a
# result of 1 row x 5 cols whose cell holds a multi-MB JSON payload sailed past
# the byte budget (the first row is admitted unconditionally, see below) and put
# ~1.1M tokens into one observation. Cells are clamped first so both the row
# budget and the mandatory-first-row guarantee stay bounded.
MAX_CELL_CHARS = 1_000

_ELISION = "…[truncated {dropped} chars]"


def clamp_scalar(value: Any, max_chars: int) -> Any:
    """Clamp one cell/stat value to *max_chars*, leaving non-strings that are
    already short (numbers, bools, None) untouched."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else None
    if text is None:
        try:
            text = json.dumps(value, default=str, ensure_ascii=False)
        except Exception:
            text = str(value)
    if len(text) <= max_chars:
        return value if isinstance(value, str) else text
    return text[:max_chars] + _ELISION.format(dropped=len(text) - max_chars)


def _clamp_row(row: Any, max_chars: int) -> Any:
    if isinstance(row, dict):
        return {k: clamp_scalar(v, max_chars) for k, v in row.items()}
    if isinstance(row, list):
        return [clamp_scalar(v, max_chars) for v in row]
    return clamp_scalar(row, max_chars)


def clamp_stats(info: Dict[str, Any], max_chars: int = MAX_CELL_CHARS) -> Dict[str, Any]:
    """Clamp verbatim cell values echoed by ``df.describe(include='all')``.

    ``column_info`` carries ``top``/``min``/``max``/percentiles, which are raw
    cells — for a wide column that reproduces the very payload the preview just
    clamped. gate_stats_for_privacy only runs when allow_llm_see_data is off, so
    without this the stats block is unbounded on the common path.
    """
    if not isinstance(info, dict):
        return info
    out = {k: v for k, v in info.items() if k != "column_info"}
    cols_out: Dict[str, Any] = {}
    for col, ci in (info.get("column_info") or {}).items():
        cols_out[col] = (
            {k: clamp_scalar(v, max_chars) for k, v in ci.items()}
            if isinstance(ci, dict)
            else clamp_scalar(ci, max_chars)
        )
    if "column_info" in info:
        out["column_info"] = cols_out
    return out


def _row_bytes(row: Any) -> int:
    # +2 accounts for the inter-row ", " separator in the serialized list, so the
    # summed estimate stays at or above the real list size (budget is a ceiling).
    try:
        return len(json.dumps(row, default=str, ensure_ascii=False).encode("utf-8")) + 2
    except Exception:
        return len(str(row).encode("utf-8")) + 2


# Per-column stat keys that are pure structure/aggregates — they never reproduce
# an individual raw cell value, so they are safe to share even when the LLM is
# not allowed to see data.
_SAFE_COLUMN_STAT_KEYS = (
    "dtype", "non_null_count", "null_count", "unique_count", "count", "unique",
    "mean", "std",
)


def gate_stats_for_privacy(info: Dict[str, Any]) -> Dict[str, Any]:
    """Strip raw cell values from a ``get_df_info`` stats dict for privacy mode.

    ``df.describe(include='all')`` populates ``column_info`` with values that are
    *verbatim cells*: ``top`` (most-frequent value), ``min``/``max`` and the
    percentiles. These must not reach the LLM when ``allow_llm_see_data`` is off.

    Kept (never echo a single row): structural counts and derived aggregates
    (mean/std, plus an exact ``sum`` derived as mean*count). Date/time columns
    additionally keep ``min``/``max`` as a low-sensitivity time-range. Categorical
    columns keep only structural counts (``top``/``freq`` are dropped).
    """
    if not isinstance(info, dict):
        return info

    safe: Dict[str, Any] = {
        k: info[k]
        for k in ("total_rows", "total_columns", "memory_usage", "dtypes_count")
        if k in info
    }

    cols_out: Dict[str, Any] = {}
    for col, ci in (info.get("column_info") or {}).items():
        if not isinstance(ci, dict):
            cols_out[col] = ci
            continue
        dtype = str(ci.get("dtype", ""))
        safe_ci: Dict[str, Any] = {k: ci[k] for k in _SAFE_COLUMN_STAT_KEYS if k in ci}
        if "datetime" in dtype or "date" in dtype:
            # Time extent (range) is an explicit allowed exception — useful for
            # reasoning about windows, low sensitivity.
            for k in ("min", "max"):
                if k in ci:
                    safe_ci[k] = ci[k]
        else:
            # Numeric columns: expose an exact aggregate sum (= mean * count)
            # without revealing min/max/percentiles.
            mean, count = ci.get("mean"), ci.get("count")
            if (
                isinstance(mean, (int, float)) and not isinstance(mean, bool)
                and isinstance(count, (int, float)) and not isinstance(count, bool)
            ):
                safe_ci["sum"] = mean * count
        cols_out[col] = safe_ci

    safe["column_info"] = cols_out
    return safe


def build_data_preview(
    formatted: Dict[str, Any],
    *,
    budget_bytes: int = DEFAULT_PREVIEW_BUDGET_BYTES,
    allow_llm_see_data: bool = True,
    max_cell_chars: int = MAX_CELL_CHARS,
) -> Dict[str, Any]:
    """Build a budgeted, self-describing preview from a ``format_df_for_widget`` dict.

    Args:
        formatted: ``{"rows": [...], "columns": [...], "info": {...}}``.
        budget_bytes: max serialized size of the included rows.
        allow_llm_see_data: when False, return only columns + row_count + stats.

    Returns a dict with ``columns`` and (when data is visible) ``rows`` plus:
        - ``row_count``: true total row count.
        - ``truncated``: whether rows were dropped to fit the budget.
        - ``note`` (truncated only): human-readable description of the cut.
    """
    columns = formatted.get("columns", []) or []
    raw_rows = formatted.get("rows", []) or []
    info = formatted.get("info", {}) or {}
    total = info.get("total_rows")
    if not isinstance(total, int):
        total = len(raw_rows)

    # Clamp cell width before any byte accounting: the row budget below bounds
    # row *count*, and the head loop admits the first row unconditionally, so an
    # unclamped wide cell escapes the budget entirely.
    rows = [_clamp_row(r, max_cell_chars) for r in raw_rows]
    cells_truncated = rows != raw_rows

    if not allow_llm_see_data:
        return {
            "columns": [{"field": c.get("field")} for c in columns if isinstance(c, dict)],
            "row_count": total,
            "stats": clamp_stats(gate_stats_for_privacy(info), max_cell_chars),
            "data_hidden": True,
            "note": (
                "Row-level data is hidden by organization policy "
                "(allow_llm_see_data is off). Only columns, row_count, and "
                "aggregate stats are available — do not attempt to retrieve raw "
                "values (e.g. via inspect_data); reason from the structure and "
                "aggregates provided."
            ),
        }

    # Does the whole result fit the budget?
    used = 0
    for row in rows:
        used += _row_bytes(row)
        if used > budget_bytes:
            break
    else:
        out = {
            "columns": columns,
            "rows": rows,
            "row_count": total,
            "truncated": False,
            "cells_truncated": cells_truncated,
        }
        if cells_truncated:
            # Say so explicitly: without a note the planner reads a clipped cell
            # as the whole value and reasons from a truncated payload.
            out["note"] = (
                f"all {total} rows shown; long cell values clipped to "
                f"{max_cell_chars} chars"
            )
        return out

    # Truncated: keep head (~75% of budget) + tail (remainder). create_data
    # results are usually sorted, so the tail carries as much signal as the head.
    # Head and tail are measured against their own budgets so the combined size
    # never exceeds budget_bytes even when later rows serialize larger.
    head_budget = (budget_bytes * 3) // 4
    head_rows: List[Any] = []
    used = 0
    for row in rows:
        b = _row_bytes(row)
        if head_rows and used + b > head_budget:
            break
        if not head_rows and b > budget_bytes:
            # The first row is always admitted so a preview is never empty, but a
            # very wide row (many columns, each at the cell cap) must not blow the
            # budget through that door — re-clamp it to a share of the budget.
            n_fields = len(row) if isinstance(row, (dict, list)) and row else 1
            row = _clamp_row(row, max(64, budget_bytes // n_fields))
            b = _row_bytes(row)
            cells_truncated = True
        head_rows.append(row)
        used += b

    tail_rows: List[Any] = []
    i = len(rows) - 1
    while i >= len(head_rows):
        b = _row_bytes(rows[i])
        if used + b > budget_bytes:
            break
        tail_rows.append(rows[i])
        used += b
        i -= 1
    tail_rows.reverse()

    preview_rows = head_rows + tail_rows
    head_n, tail_n = len(head_rows), len(tail_rows)
    if tail_n > 0:
        note = f"showing first {head_n} and last {tail_n} of {total} rows"
    else:
        note = f"showing first {head_n} of {total} rows"
    if cells_truncated:
        note += f"; long cell values clipped to {max_cell_chars} chars"
    return {
        "columns": columns,
        "rows": preview_rows,
        "row_count": total,
        "truncated": True,
        "cells_truncated": cells_truncated,
        "note": note,
    }
