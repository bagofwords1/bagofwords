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
    rows = formatted.get("rows", []) or []
    info = formatted.get("info", {}) or {}
    total = info.get("total_rows")
    if not isinstance(total, int):
        total = len(rows)

    if not allow_llm_see_data:
        return {
            "columns": [{"field": c.get("field")} for c in columns if isinstance(c, dict)],
            "row_count": total,
            "stats": gate_stats_for_privacy(info),
        }

    # Does the whole result fit the budget?
    used = 0
    for row in rows:
        used += _row_bytes(row)
        if used > budget_bytes:
            break
    else:
        return {
            "columns": columns,
            "rows": rows,
            "row_count": total,
            "truncated": False,
        }

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
    return {
        "columns": columns,
        "rows": preview_rows,
        "row_count": total,
        "truncated": True,
        "note": note,
    }
