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
import logging
import math
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# Column sums for model-facing previews
#
# A neutral ``column_sums`` block, ``{"note": "sum = sum of all N rows",
# "sum": {field: value}}``: the exact sum over all rows of each numeric column,
# so the model never adds up a table itself. There is no instruction attached;
# whether a sum means anything stays the model's judgement, as it is for the
# ``mean`` it already gets. Guards: no sum on identifier or year-like columns,
# and no block when the rows plainly hold a total row (a total label, or the
# ``k x max`` signature). Where the rows at hand are incomplete, a missing label
# proves nothing, so the ``k x max`` check decides and fails safe.
# ---------------------------------------------------------------------------

SUMS_MAX_COLUMNS = 10
SUMS_ROW_SCAN_LIMIT = 1000

_ID_TOKENS = frozenset({
    "id", "ids", "uid", "uuid", "guid", "key", "pk", "fk", "code", "sku", "ean",
    "upc", "isbn", "iban", "zip", "zipcode", "postcode", "phone",
})
_ID_LAST_TOKENS = frozenset({"no", "nr", "nbr", "num", "number"})
_ID_SUFFIX_EXCEPTIONS = (
    "aid", "oid", "fluid", "liquid", "squid", "valid", "covid", "hybrid", "acid",
    "solid", "rapid", "humid", "vivid", "rigid", "bid",
)
_CALENDAR_TOKENS = frozenset({
    "year", "yr", "yyyy", "fy", "quarter", "qtr", "month", "week", "wk", "weekday",
    "day", "date", "hour", "period", "שנה", "שנת", "רבעון", "חודש", "שבוע", "יום",
    "תאריך",
})
_HINT_TOKENS = frozenset({
    "count", "counts", "cnt", "num", "number", "total", "totals", "sum", "amount",
    "amt", "cost", "costs", "spend", "spent", "revenue", "revenues", "sales",
    "income", "expense", "expenses", "profit", "budget", "actual", "paid", "fee",
    "fees", "tax", "taxes", "qty", "quantity", "units", "orders", "shipments",
    "hours", "eur", "usd", "ils", "nis", "gbp",
})
_HEBREW_HINTS = ("סכום", "עלות", "הכנסה", "הוצאה", "תקציב", "כמות", "שעות")

_TOTAL_LABEL_RE = re.compile(r"(?<![a-z0-9])(?:sub|grand[ _-]?)?totals?(?![a-z0-9])")
_SUMMARY_LABELS = frozenset({"all", "(all)", "average", "avg", "avg.", "mean", "ממוצע"})
_HEBREW_TOTAL_LABELS = ('סה"כ', "סה״כ", "סה''כ", "סך הכל", "סך הכול")
_MAX_LABEL_CHARS = 64

_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")
_LETTER_DIGIT_RE = re.compile(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])")

# to_json writes at most 10 decimals (its double_precision default).
_MAX_DECIMALS = 10
# mean x count is a float: past this it can be off by whole units (2**53 ~ 9e15).
_MAX_FLOAT_SUM = 1e15


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value: Any) -> bool:
    if not _is_number(value):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int past the float range
        return False


def _tokens(field: str) -> List[str]:
    text = _LETTER_DIGIT_RE.sub(" ", _CAMEL_RE.sub(" ", field))
    text = "".join(ch if ch.isalnum() else " " for ch in text)
    return [t.casefold() for t in text.split()]


def _is_identifier(field: str, tokens: List[str]) -> bool:
    if any(t in _ID_TOKENS for t in tokens):
        return True
    if "מזהה" in field or "קוד" in tokens:
        return True
    if not tokens:
        return False
    last = tokens[-1]
    if last in _ID_LAST_TOKENS:
        return True
    return len(last) >= 4 and last.endswith("id") and not last.endswith(_ID_SUFFIX_EXCEPTIONS)


def _is_whole(value: Any) -> bool:
    return _finite(value) and float(value).is_integer()


def _is_year_like(tokens: List[str], lo: Any, hi: Any) -> bool:
    if any(t in _CALENDAR_TOKENS for t in tokens):
        return True
    return _is_whole(lo) and _is_whole(hi) and 1900 <= lo <= hi <= 2100


def _has_hint(field: str, tokens: List[str]) -> bool:
    return any(t in _HINT_TOKENS for t in tokens) or any(h in field for h in _HEBREW_HINTS)


def _round_sum(s: Any, integral: bool) -> Any:
    if integral:
        # A whole Decimal sum is exact; a float one (mean x count) needs rounding.
        return int(s) if isinstance(s, Decimal) else int(round(s))
    if abs(s) >= 1e11:
        return int(round(s))
    if abs(s) >= 1:
        r = round(s, 2)
        return int(r) if r == int(r) else float(r)
    return float(f"{s:.4g}")


def _fields(columns: Any, column_info: Dict[str, Any]) -> List[str]:
    fields: List[str] = []
    if isinstance(columns, list) and columns:
        for entry in columns:
            if isinstance(entry, dict):
                field = entry.get("field")
                if field is None or field == "":
                    continue
                fields.append(str(field))
            elif isinstance(entry, str) and entry:
                fields.append(entry)
    else:
        fields = list(column_info)
    seen = set()
    return [f for f in fields if not (f in seen or seen.add(f))]


def _numeric_column(ci: Any) -> Optional[Tuple[float, bool, float, float]]:
    """(sum, integral, min, max) of a numeric column from its describe() stats."""
    if not isinstance(ci, dict):
        return None
    dtype = str(ci.get("dtype", "")).lower()
    if not dtype.startswith(("int", "uint", "float")):
        return None
    mean, count, lo, hi = ci.get("mean"), ci.get("count"), ci.get("min"), ci.get("max")
    if not all(_finite(v) for v in (mean, count, lo, hi)) or count < 1:
        return None
    s = mean * count
    # A small net sum is not enough: [1e16, 1, -1e16] has a describe() mean of
    # 0. Bounding every value bounds the error accumulated while summing.
    if abs(s) >= _MAX_FLOAT_SUM or max(abs(lo), abs(hi)) * count >= _MAX_FLOAT_SUM:
        return None
    return s, dtype.startswith(("int", "uint")), lo, hi


def _object_column_stats(
    rows: List[Any], field: str, non_null_count: Any
) -> Optional[Tuple[Decimal, bool, float, float]]:
    """(exact sum, integral, min, max) of an object column whose cells are numbers.

    Object columns are Decimal results from clients that build the frame from
    raw rows; the stored rows hold them as JSON numbers. Only called with the
    complete rows. Numeric strings are never parsed.
    """
    if not _is_number(non_null_count):
        return None
    values: List[Decimal] = []
    for row in rows:
        cell = row.get(field)
        if cell is None:
            continue
        if not _finite(cell) or abs(cell) >= 1e15:
            return None
        values.append(Decimal(cell) if isinstance(cell, int) else Decimal(repr(cell)))
    if not values or len(values) != non_null_count:
        return None
    integral = all(v == v.to_integral_value() for v in values)
    return sum(values, Decimal(0)), integral, float(min(values)), float(max(values))


def _has_total_label(scan: List[Any]) -> bool:
    for row in scan:
        cells = row.values() if isinstance(row, dict) else row if isinstance(row, list) else ()
        for cell in cells:
            if not isinstance(cell, str):
                continue
            text = cell.strip()
            if not text or len(text) > _MAX_LABEL_CHARS:
                continue
            folded = text.casefold()
            if _TOTAL_LABEL_RE.search(folded):
                return True
            bare = folded[:-1].strip() if folded.endswith(":") else folded
            if bare in _SUMMARY_LABELS:
                return True
            if any(h in text for h in _HEBREW_TOTAL_LABELS):
                return True
    return False


def _decimals(value: Any) -> int:
    if isinstance(value, int):
        return 0
    text = repr(value)
    if "e" in text or "E" in text:
        return _MAX_DECIMALS
    if "." not in text:
        return 0
    return min(len(text.split(".", 1)[1].rstrip("0")), _MAX_DECIMALS)


def _column_decimals(scan: List[Any], field: str) -> Optional[int]:
    """Most decimal places among the column's JSON-number cells, or None if none."""
    d: Optional[int] = None
    for row in scan:
        if isinstance(row, dict):
            v = row.get(field)
            if _finite(v):
                d = max(d or 0, _decimals(v))
    return d


def _matches_k_max(s: float, hi: float, d: Optional[int], row_count: int) -> bool:
    rounding = 0.0 if d is None else min(0.5 * 10 ** -d * (row_count + 3), 0.01 * hi)
    tol = max(1e-9 * abs(s), rounding)
    return any(abs(s - k * hi) <= tol for k in (2, 3, 4))


def _maxima_on_one_row(scan: List[Any], cols: List[Tuple[str, float]]) -> bool:
    for row in scan:
        if not isinstance(row, dict):
            continue
        if all(
            _finite(row.get(f)) and abs(row[f] - hi) <= max(1e-9 * abs(hi), 1e-10)
            for f, hi in cols
        ):
            return True
    return False


def _total_row_suspected(
    eligible: List[Tuple[str, Any, bool, Any, Any]],
    row_count: int,
    scan: List[Any],
    complete: bool,
) -> bool:
    """S6: the ``k x max`` signature of a total row among non-negative columns."""
    non_negative = [(f, s, hi) for f, s, _, lo, hi in eligible if lo >= 0 and hi > 0]
    matching = [
        (f, hi) for f, s, hi in non_negative
        if _matches_k_max(float(s), float(hi), _column_decimals(scan, f), row_count)
    ]
    if not complete:
        return not non_negative or bool(matching)
    if not matching:
        return False
    if len(non_negative) == 1:
        return True
    if len(matching) == 1:
        return False
    return _maxima_on_one_row(scan, matching)


def column_sums(
    info: Optional[Dict[str, Any]],
    columns: Optional[List[Any]],
    row_count: int,
    rows: Optional[List[Any]] = None,
) -> Optional[Dict[str, Any]]:
    """The ``column_sums`` block for a result, or None.

    *info* is a ``get_df_info`` dict (live or stored), *columns* the widget
    columns (only ``field`` is read), *row_count* the result's true row count
    and *rows* whatever rows are at hand (all of them, or a prefix). Returns
    ``{"note": "sum = sum of all N rows", "sum": {field: value}}``, or None when
    there are fewer than 2 rows, a total-row check fires, or no column is
    eligible. Never logs.
    """
    if not isinstance(info, dict) or not _is_number(row_count) or row_count < 2:
        return None
    raw_column_info = info.get("column_info")
    if not isinstance(raw_column_info, dict):
        return None
    column_info = {str(k): v for k, v in raw_column_info.items()}

    scan = rows[:SUMS_ROW_SCAN_LIMIT] if isinstance(rows, list) else []
    complete = (
        isinstance(rows, list)
        and len(rows) == row_count
        and row_count <= SUMS_ROW_SCAN_LIMIT
        and all(isinstance(r, dict) for r in scan)
    )

    eligible: List[Tuple[str, Any, bool, Any, Any]] = []
    for field in _fields(columns, column_info):
        ci = column_info.get(field)
        stats = _numeric_column(ci)
        is_object = isinstance(ci, dict) and str(ci.get("dtype", "")).lower() == "object"
        if stats is None and is_object and complete:
            # Decimal cells: an exact sum, only from the complete rows.
            stats = _object_column_stats(scan, field, ci.get("non_null_count"))
        if stats is None:
            continue
        s, integral, lo, hi = stats
        tokens = _tokens(field)
        if _is_identifier(field, tokens):
            continue
        if _is_year_like(tokens, lo, hi) and not _has_hint(field, tokens):
            continue
        eligible.append((field, s, integral, lo, hi))
    if not eligible:
        return None

    if _has_total_label(scan):
        return None
    if _total_row_suspected(eligible, row_count, scan, complete):
        return None

    sums = {f: _round_sum(s, integral) for f, s, integral, _, _ in eligible[:SUMS_MAX_COLUMNS]}
    return {"note": f"sum = sum of all {row_count} rows", "sum": sums}


def with_column_sums(
    preview: Optional[Dict[str, Any]],
    data: Optional[Dict[str, Any]],
    *,
    call_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """A copy of a visible *preview* with a ``column_sums`` block, or *preview* itself.

    *data* is a ``format_df_for_widget`` dict or a stored step's ``data``. Hidden
    previews, non-int row counts and results the rule declines come back
    unchanged (the same object). Logs one INFO line, with the tool call id and
    two counts only, when a block is added. Never raises: on an error it logs
    one WARNING (tool call id and error type) and returns *preview* unchanged.

    *call_id* is ``runtime_ctx["tool_call_id"]`` (the ToolExecution id), not a
    step id: create_data and describe_entity build their observation before
    the orchestrator's step id reaches the tool, and the ToolExecution row
    links the call to its step (``created_step_id``) and its run.
    """
    try:
        if not isinstance(preview, dict) or not isinstance(data, dict):
            return preview
        if preview.get("data_hidden"):
            return preview
        row_count = preview.get("row_count")
        if not isinstance(row_count, int) or isinstance(row_count, bool):
            return preview
        block = column_sums(data.get("info"), data.get("columns"), row_count, data.get("rows"))
        if block is None:
            return preview
        logger.info(
            "column sums added: call=%s rows=%d columns=%d",
            call_id, row_count, len(block["sum"]),
        )
        return {**preview, "column_sums": block}
    except Exception as e:
        _log_sums_skipped("call", call_id, e)
        return preview


def guarded_column_sums(
    info: Optional[Dict[str, Any]],
    columns: Optional[List[Any]],
    row_count: int,
    rows: Optional[List[Any]] = None,
    *,
    step_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """``column_sums``, or None with one WARNING (step id and error type only) if it raises."""
    try:
        return column_sums(info, columns, row_count, rows)
    except Exception as e:
        _log_sums_skipped("step", step_id, e)
        return None


def _log_sums_skipped(id_kind: str, ref: Optional[str], error: Exception) -> None:
    # No message, field names or values: they can carry result data.
    logger.warning("column sums skipped: %s=%s error=%s", id_kind, ref, type(error).__name__)
