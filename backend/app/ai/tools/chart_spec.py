"""Deterministic chart-spec derivation, with LLM inference as a refinement.

Why this module exists
----------------------
``create_data`` used to build its visualization entirely from one LLM call: the
model returned a JSON ``data_model`` and everything downstream trusted it. Any
defect in that single reply — a column name that doesn't exist, a ``group_by``
echoing the prompt's own ``"column_name_or_null"`` placeholder, an unescaped
quote inside a column name, a ``value`` that arrived as a list — produced a
``data_model`` that named the requested chart type with nothing renderable in
it. The chart came out empty while the data tab showed rows, and every failure
was swallowed. See ``docs/feedback-loops/infer-viz-empty-chart.md``.

The shape of the fix is an inversion:

1. :func:`build_chart_spec` derives a **complete, valid** spec from the result
   set alone. It is a pure function of the rows and columns — it can only ever
   reference columns that exist, so it cannot produce an unrenderable chart.
2. :func:`apply_inference_overrides` layers the LLM's answer on top **one field
   at a time**, and only when that field resolves to a real column. An
   unresolvable suggestion is dropped and the deterministic value stands.

So a model defect costs one field, not the whole chart. The worst case is a
less insightful chart, never an empty one.

The column-classification rules here are the single implementation of logic
that previously existed three times over (``_pick_value_column`` in
``create_data.py``, ``inferDefaultSeries`` in ``EChartsVisual.vue``,
``isProbablyNumeric`` in ``VisualizationConfigEditor.vue``).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cell / column primitives
# ---------------------------------------------------------------------------

_CURRENCY_SEPARATOR_RE = re.compile(r"[₪$€£¥,\s]")
_NUMERIC_STRING_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")
_DATE_STRING_RE = re.compile(
    r"^\d{4}-\d{1,2}-\d{1,2}([ T].*)?$"      # 2025-12-22 / ISO
    r"|^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$"     # 23.06.2026, 6/23/26
    r"|^\d{4}[./]\d{1,2}[./]\d{1,2}$"         # 2026.06.23
)
_TIME_COLUMN_NAME_RE = re.compile(
    r"(date|time|day|month|week|year|period|timestamp|quarter|"
    r"תאריך|חודש|שנה|יום|שבוע|רבעון)",
    re.IGNORECASE,
)
# Numeric columns that are identifiers rather than measures.
_ID_COLUMN_NAME_RE = re.compile(r"(^|[_\s])(id|uuid|guid|key|code|zip|postal)$", re.IGNORECASE)

# Strings models emit when they mean "nothing here". They are not column names,
# but `group_by: "none"` used to sail through as one and silence the chart.
_NULLISH_TOKENS = {
    "", "none", "null", "nil", "n/a", "na", "-", "undefined", "false",
    "column_name_or_null", "column_name", "<column>", "string",
}

_VALID_AGGREGATIONS = {"sum", "avg", "count", "min", "max"}

# Chart types whose renderer shows exactly one row.
SINGLE_VALUE_CARD_TYPES = {"count", "metric_card"}

# Required encoding fields per chart type. A spec missing any of these cannot be
# drawn, so it is demoted to `table` rather than shipped as an empty canvas.
_REQUIRED_FIELDS: Dict[str, Tuple[str, ...]] = {
    "bar_chart": ("key", "value"),
    "line_chart": ("key", "value"),
    "area_chart": ("key", "value"),
    "pie_chart": ("key", "value"),
    "map": ("key", "value"),
    "scatter_plot": ("x", "y"),
    "heatmap": ("x", "y", "value"),
    "candlestick": ("key", "open", "close", "low", "high"),
    "treemap": ("value",),
    "radar_chart": ("dimensions",),
    "count": ("value",),
    "metric_card": ("value",),
}

_CARTESIAN_TYPES = {"bar_chart", "line_chart", "area_chart"}


def norm_text(s: Any) -> str:
    return str(s).strip().lower() if s is not None else ""


def parse_numeric_like(value: Any) -> Optional[float]:
    """Parse a raw or display-formatted numeric cell.

    Accepts real numbers and strings like ``"1234"``, ``"₪29,134,139"``,
    ``"4,125.04"``, ``"45.2%"``. Returns ``None`` for anything else (dates,
    labels, mixed text) so callers can tell a measure from a label reliably.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    s = _CURRENCY_SEPARATOR_RE.sub("", s)
    if s.endswith("%"):
        s = s[:-1]
    if not _NUMERIC_STRING_RE.match(s):
        return None
    try:
        return float(s)
    except Exception:
        return None


def looks_like_date_string(value: Any) -> bool:
    return isinstance(value, str) and bool(_DATE_STRING_RE.match(value.strip()))


def column_fields(formatted: Dict[str, Any]) -> List[str]:
    """Column names in result order (the order the query selected them)."""
    return [
        c.get("field")
        for c in ((formatted or {}).get("columns") or [])
        if isinstance(c, dict) and c.get("field")
    ]


def column_cells(rows: List[Dict[str, Any]], col: str, cap: int = 50) -> List[Any]:
    out = []
    for r in rows[:cap]:
        if isinstance(r, dict) and r.get(col) is not None:
            out.append(r.get(col))
    return out


def numeric_like_ratio(cells: List[Any]) -> float:
    if not cells:
        return 0.0
    return sum(1 for c in cells if parse_numeric_like(c) is not None) / len(cells)


# ---------------------------------------------------------------------------
# Column classification
# ---------------------------------------------------------------------------


class ColumnProfile:
    """Per-column facts derived from the actual result set.

    ``temporal`` wins over ``numeric``: a ``year`` column holds numbers but
    belongs on an axis, not in a measure.
    """

    __slots__ = ("name", "temporal", "numeric", "categorical", "unique_count", "non_empty")

    def __init__(self, name: str, temporal: bool, numeric: bool, unique_count: int, non_empty: int):
        self.name = name
        self.temporal = temporal
        self.numeric = numeric and not temporal
        self.categorical = not temporal and not self.numeric
        self.unique_count = unique_count
        self.non_empty = non_empty

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        kind = "temporal" if self.temporal else ("numeric" if self.numeric else "categorical")
        return f"<Column {self.name!r} {kind} uniq={self.unique_count}>"


def profile_columns(formatted: Dict[str, Any]) -> List[ColumnProfile]:
    rows = (formatted or {}).get("rows") or []
    column_info = ((formatted or {}).get("info") or {}).get("column_info") or {}
    profiles: List[ColumnProfile] = []

    for name in column_fields(formatted):
        cells = column_cells(rows, name)
        dtype = str((column_info.get(name) or {}).get("dtype") or "")

        temporal = bool(_TIME_COLUMN_NAME_RE.search(str(name)))
        if not temporal and ("datetime" in dtype or dtype == "date"):
            temporal = True
        if not temporal and cells:
            if sum(1 for c in cells if looks_like_date_string(c)) / len(cells) >= 0.8:
                temporal = True

        numeric = bool(cells) and numeric_like_ratio(cells) >= 0.5

        declared_unique = (column_info.get(name) or {}).get("unique_count")
        if isinstance(declared_unique, int) and declared_unique > 0:
            unique_count = declared_unique
        else:
            unique_count = len({str(c) for c in cells})

        profiles.append(ColumnProfile(str(name), temporal, numeric, unique_count, len(cells)))

    return profiles


def resolve_column(candidate: Any, columns: List[str]) -> Optional[str]:
    """Map a model-supplied name onto a real column, or ``None``.

    Tolerates the ways a model mangles a name it was shown — case, surrounding
    or internal whitespace, and quote characters (Hebrew ``סה"כ`` is routinely
    echoed back with the gershayim dropped or swapped for ``״``). Anything that
    still doesn't land on a real column is rejected: guessing further is how a
    chart ends up pointing at a column that isn't there.
    """
    if candidate is None or not isinstance(candidate, (str, int, float)):
        return None
    raw = str(candidate).strip()
    if not raw or raw.lower() in _NULLISH_TOKENS:
        return None

    for col in columns:
        if str(col) == raw:
            return col

    def squash(s: str) -> str:
        s = re.sub(r"[\"'`״׳]", "", str(s))
        s = re.sub(r"\s+", " ", s)
        return s.strip().casefold()

    target = squash(raw)
    if not target:
        return None
    for col in columns:
        if squash(col) == target:
            return col
    return None


# ---------------------------------------------------------------------------
# Deterministic derivation
# ---------------------------------------------------------------------------


def _pick_axis(profiles: List[ColumnProfile], exclude: Tuple[str, ...] = ()) -> Optional[str]:
    """Axis column: a time column if there is one, else the first category."""
    for p in profiles:
        if p.name in exclude:
            continue
        if p.temporal:
            return p.name
    for p in profiles:
        if p.name in exclude:
            continue
        if p.categorical and p.non_empty:
            return p.name
    return None


def _pick_measure(profiles: List[ColumnProfile], exclude: Tuple[str, ...] = ()) -> Optional[str]:
    """Measure column: first non-id numeric in result order.

    Result order is the query's SELECT order, and the coder is instructed to put
    the metric the user asked for up front — so "first" is a better guess than
    any name-matching heuristic, and it never invents a column.
    """
    numerics = [p for p in profiles if p.numeric and p.name not in exclude and p.non_empty]
    for p in numerics:
        if not _ID_COLUMN_NAME_RE.search(p.name):
            return p.name
    return numerics[0].name if numerics else None


def _pick_breakdown(
    profiles: List[ColumnProfile],
    key: Optional[str],
    row_count: int,
) -> Optional[str]:
    """Detect a genuine breakdown dimension — conservatively.

    Only when the axis column actually repeats and a low-cardinality category
    explains the repetition (``unique(key) * unique(cat)`` accounts for the
    rows). A wrong ``group_by`` empties a chart, so silence beats a guess.
    """
    if not key or row_count < 4:
        return None
    key_profile = next((p for p in profiles if p.name == key), None)
    if not key_profile or key_profile.unique_count >= row_count:
        return None  # one row per axis value: nothing to break down

    for p in profiles:
        if p.name == key or not p.categorical or not p.non_empty:
            continue
        if not (2 <= p.unique_count <= 20):
            continue
        expected = key_profile.unique_count * p.unique_count
        if expected and abs(expected - row_count) <= max(1, row_count * 0.1):
            return p.name
    return None


def _needs_aggregation(
    profiles: List[ColumnProfile],
    key: Optional[str],
    group_by: Optional[str],
    row_count: int,
) -> bool:
    """True when several rows land in one bucket, so first-row-wins is wrong."""
    if not key or not row_count:
        return False
    key_profile = next((p for p in profiles if p.name == key), None)
    if not key_profile or not key_profile.unique_count:
        return False
    buckets = key_profile.unique_count
    if group_by:
        gp = next((p for p in profiles if p.name == group_by), None)
        if gp and gp.unique_count:
            buckets *= gp.unique_count
    return row_count > buckets


def build_chart_spec(formatted: Dict[str, Any], requested_type: Optional[str]) -> Dict[str, Any]:
    """Derive a complete, renderable ``data_model`` from the result set alone.

    No LLM, no network, no I/O. Every column it names is one the query actually
    returned, so the output is always renderable — or explicitly ``table`` when
    the requested chart cannot be built from this data.
    """
    chart_type = (requested_type or "table").strip().lower() or "table"
    spec: Dict[str, Any] = {"type": chart_type, "series": []}
    if chart_type == "table":
        return spec

    rows = (formatted or {}).get("rows") or []
    profiles = profile_columns(formatted)
    if not profiles or not rows:
        return spec  # nothing to derive from; renderability check demotes later

    row_count = ((formatted or {}).get("info") or {}).get("total_rows") or len(rows)
    numerics = [p for p in profiles if p.numeric and p.non_empty]
    categoricals = [p for p in profiles if p.categorical and p.non_empty]

    if chart_type in SINGLE_VALUE_CARD_TYPES:
        value = _pick_measure(profiles)
        if value is None and len(profiles) == 1:
            value = profiles[0].name  # a deliberate single-column result
        if value is not None:
            entry: Dict[str, Any] = {"name": value, "value": value}
            axis = _pick_axis(profiles, exclude=(value,))
            axis_profile = next((p for p in profiles if p.name == axis), None)
            if len(rows) > 1:
                if axis_profile is not None and axis_profile.temporal:
                    # Latest-value + sparkline: the legacy multi-row card shape.
                    entry["sparkline_column"] = value
                    entry["sparkline_x"] = axis
                else:
                    entry["aggregation"] = "sum"
            spec["series"] = [entry]
        return spec

    if chart_type == "scatter_plot":
        if len(numerics) >= 2:
            spec["series"] = [{"name": numerics[1].name, "x": numerics[0].name, "y": numerics[1].name}]
        return spec

    if chart_type == "heatmap":
        value = _pick_measure(profiles)
        axes = [p for p in categoricals if p.name != value][:2]
        if len(axes) < 2:
            axes = [p for p in profiles if p.name != value and not p.numeric][:2]
        if value and len(axes) == 2:
            spec["series"] = [{
                "name": value,
                "x": axes[0].name,
                "y": axes[1].name,
                "value": value,
                "colorScheme": "blue",
                "showValues": True,
            }]
        return spec

    if chart_type == "pie_chart":
        value = _pick_measure(profiles)
        pie_candidates = [p for p in categoricals if p.name != value and 1 <= p.unique_count <= 50]
        category = pie_candidates[0].name if pie_candidates else _pick_axis(profiles, exclude=(value,) if value else ())
        if category and value:
            entry = {"name": value, "key": category, "value": value}
            if _needs_aggregation(profiles, category, None, row_count):
                entry["aggregation"] = "sum"
            spec["series"] = [entry]
        return spec

    if chart_type in _CARTESIAN_TYPES:
        key = _pick_axis(profiles)
        value = _pick_measure(profiles, exclude=(key,) if key else ())
        if key is None and value is not None:
            key = next((p.name for p in profiles if p.name != value), None)
        if key is None or value is None:
            return spec
        group_by = _pick_breakdown(profiles, key, row_count)
        entry = {"name": value, "key": key, "value": value}
        if _needs_aggregation(profiles, key, group_by, row_count):
            entry["aggregation"] = "sum"
        spec["series"] = [entry]
        if group_by:
            spec["group_by"] = group_by
        return spec

    # map / candlestick / treemap / radar_chart: no reliable derivation. The
    # spec stays empty and either the LLM supplies a valid encoding or the
    # renderability check demotes it to a table.
    if chart_type == "map":
        key = _pick_axis(profiles)
        value = _pick_measure(profiles, exclude=(key,) if key else ())
        if key and value:
            spec["series"] = [{"name": value, "key": key, "value": value}]
    return spec


# ---------------------------------------------------------------------------
# LLM overrides
# ---------------------------------------------------------------------------


def _series_entries(inferred: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The model's series, normalized to a list of dicts (it emits both)."""
    raw = inferred.get("series")
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict)]
    return []


def _override_values(entries: List[Dict[str, Any]], columns: List[str]) -> List[Tuple[str, Dict[str, Any]]]:
    """Resolve every measure the model named, keeping the ones that are real.

    Handles ``value`` arriving as a list (a plausible way to ask for two
    measures, and previously fatal to the whole reply).
    """
    out: List[Tuple[str, Dict[str, Any]]] = []
    for entry in entries:
        raw = entry.get("value", entry.get("metric"))
        candidates = raw if isinstance(raw, list) else [raw]
        for cand in candidates:
            col = resolve_column(cand, columns)
            if col and col not in [c for c, _ in out]:
                out.append((col, entry))
    return out


def apply_inference_overrides(
    spec: Dict[str, Any],
    inferred: Optional[Dict[str, Any]],
    formatted: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Layer the model's suggestions onto ``spec``, one validated field at a time.

    Every column reference is resolved against the real result columns; whatever
    doesn't resolve is dropped and the deterministic value stays. Returns the
    spec plus a telemetry dict recording what was applied and what was dropped.
    """
    meta: Dict[str, Any] = {"applied": [], "dropped": []}
    if not isinstance(inferred, dict) or not inferred:
        return spec, meta

    columns = column_fields(formatted)
    rows = (formatted or {}).get("rows") or []
    out = dict(spec)
    chart_type = str(out.get("type") or "table").lower()
    base_series = [dict(s) for s in (out.get("series") or []) if isinstance(s, dict)]
    base_entry = dict(base_series[0]) if base_series else {}

    def apply(field: str, value: Any) -> None:
        meta["applied"].append(f"{field}={value}")

    def drop(field: str, value: Any, reason: str) -> None:
        meta["dropped"].append(f"{field}={value!r} ({reason})")

    entries = _series_entries(inferred)

    # --- series -------------------------------------------------------------
    if chart_type in _CARTESIAN_TYPES or chart_type in {"pie_chart", "map"}:
        key_raw = next((e.get("key", e.get("x")) for e in entries if e.get("key") or e.get("x")), None)
        if key_raw is not None:
            col = resolve_column(key_raw, columns)
            if col:
                base_entry["key"] = col
                apply("key", col)
            else:
                drop("key", key_raw, "not a column")

        resolved = _override_values(entries, columns)
        if resolved:
            new_series = []
            for col, entry in resolved:
                item: Dict[str, Any] = {
                    "name": entry.get("name") or col,
                    "key": base_entry.get("key"),
                    "value": col,
                }
                agg = entry.get("aggregation")
                if agg in _VALID_AGGREGATIONS:
                    item["aggregation"] = agg
                elif base_entry.get("aggregation") in _VALID_AGGREGATIONS:
                    item["aggregation"] = base_entry["aggregation"]
                elif agg is not None:
                    drop("aggregation", agg, "not a valid aggregation")
                new_series.append(item)
            out["series"] = new_series
            apply("value", [c for c, _ in resolved])
        else:
            raw_values = [e.get("value") for e in entries if e.get("value") is not None]
            if raw_values:
                drop("value", raw_values, "not a column")
            if base_entry:
                out["series"] = [base_entry]

    elif chart_type == "scatter_plot":
        entry = dict(base_entry)
        for field, keys in (("x", ("x", "key")), ("y", ("y", "value"))):
            raw = next((e.get(k) for e in entries for k in keys if e.get(k)), None)
            if raw is None:
                continue
            col = resolve_column(raw, columns)
            if col:
                entry[field] = col
                apply(field, col)
            else:
                drop(field, raw, "not a column")
        if entry.get("x") and entry.get("y"):
            entry.setdefault("name", entry["y"])
            out["series"] = [entry]

    elif chart_type == "heatmap":
        entry = dict(base_entry)
        for field in ("x", "y", "value"):
            raw = next((e.get(field) for e in entries if e.get(field)), None)
            if raw is None:
                continue
            col = resolve_column(raw, columns)
            if col:
                entry[field] = col
                apply(field, col)
            else:
                drop(field, raw, "not a column")
        if entry.get("x") and entry.get("y") and entry.get("value"):
            entry.setdefault("name", entry["value"])
            out["series"] = [entry]

    elif chart_type in SINGLE_VALUE_CARD_TYPES:
        entry = dict(base_entry)
        raw = next((e.get("value", e.get("metric")) for e in entries if e.get("value") or e.get("metric")), None)
        if raw is not None:
            col = resolve_column(raw if not isinstance(raw, list) else (raw[0] if raw else None), columns)
            if col:
                entry["value"] = col
                apply("value", col)
            else:
                drop("value", raw, "not a column")
        for field in ("sparkline_column", "sparkline_x", "comparison"):
            raw = next((e.get(field) for e in entries if e.get(field)), None)
            if raw is None:
                continue
            col = resolve_column(raw, columns)
            if col:
                entry[field] = col
                apply(field, col)
            else:
                drop(field, raw, "not a column")
        first = entries[0] if entries else {}
        if first.get("name"):
            entry["name"] = first["name"]
        agg = first.get("aggregation")
        if agg in _VALID_AGGREGATIONS:
            entry["aggregation"] = agg
        elif agg is not None:
            drop("aggregation", agg, "not a valid aggregation")
        if entry.get("value"):
            out["series"] = [entry]

    # --- group_by -----------------------------------------------------------
    if "group_by" in inferred:
        raw_gb = inferred.get("group_by")
        if isinstance(raw_gb, (list, tuple)):
            raw_gb = next((g for g in raw_gb if isinstance(g, str) and g), None)
        if raw_gb is None or str(raw_gb).strip().lower() in _NULLISH_TOKENS:
            if raw_gb is not None:
                drop("group_by", raw_gb, "placeholder/nullish")
            out.pop("group_by", None)
        else:
            col = resolve_column(raw_gb, columns)
            # A breakdown column with no values yields zero series — same empty
            # chart as a name that doesn't exist, so it's rejected the same way.
            if col and any(str(r.get(col, "")).strip() for r in rows if isinstance(r, dict)):
                out["group_by"] = col
                apply("group_by", col)
            else:
                drop("group_by", raw_gb, "not a column" if not col else "column is empty")
                out.pop("group_by", None)

    # --- filters ------------------------------------------------------------
    raw_filters = inferred.get("filters")
    if isinstance(raw_filters, dict):
        raw_filters = [raw_filters]
    if isinstance(raw_filters, list):
        kept = []
        for f in raw_filters:
            if not isinstance(f, dict):
                continue
            col = resolve_column(f.get("column", f.get("field")), columns)
            op = f.get("operator", f.get("op"))
            if col and op:
                kept.append({"column": col, "operator": str(op), "value": f.get("value")})
            else:
                drop("filter", f, "unresolvable column/operator")
        if kept:
            out["filters"] = kept
            apply("filters", [f["column"] for f in kept])

    return out, meta


# ---------------------------------------------------------------------------
# Renderability
# ---------------------------------------------------------------------------


def _spec_field(spec: Dict[str, Any], field: str) -> Any:
    series = [s for s in (spec.get("series") or []) if isinstance(s, dict)]
    if not series:
        return None
    if field == "value":
        return next((s.get("value") for s in series if s.get("value")), None)
    return series[0].get(field)


def ensure_renderable(spec: Dict[str, Any], columns: List[str]) -> Tuple[Dict[str, Any], Optional[str]]:
    """Demote to ``table`` when the spec can't actually be drawn.

    The renderers fail silently on a missing encoding — an empty canvas next to
    a populated data tab. A table is always a truthful rendering of the result,
    so it is the floor rather than a blank chart.
    """
    chart_type = str(spec.get("type") or "table").lower()
    required = _REQUIRED_FIELDS.get(chart_type)
    if not required:
        return spec, None

    missing = [f for f in required if not _spec_field(spec, f)]
    if missing:
        demoted = dict(spec)
        demoted["type"] = "table"
        demoted["filters"] = None
        return demoted, f"{chart_type} missing {'/'.join(missing)}"

    # Every named column must exist — belt and braces for callers that build a
    # spec by hand rather than through build_chart_spec.
    for entry in spec.get("series") or []:
        if not isinstance(entry, dict):
            continue
        for field in ("key", "value", "x", "y"):
            name = entry.get(field)
            if name and resolve_column(name, columns) is None:
                demoted = dict(spec)
                demoted["type"] = "table"
                demoted["filters"] = None
                return demoted, f"{chart_type} references unknown column {name!r}"
    return spec, None


def build_final_data_model(
    requested_type: Optional[str],
    inferred: Optional[Dict[str, Any]],
    formatted: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Deterministic spec, refined by inference, guaranteed renderable.

    Returns ``(data_model, meta)``. ``meta`` carries what inference contributed
    and what was rejected — the telemetry that makes the next regression
    diagnosable instead of invisible.
    """
    base = build_chart_spec(formatted, requested_type)
    spec, meta = apply_inference_overrides(base, inferred, formatted)

    # Presentation-only fields: no column references, so they pass through as-is
    # (`display` is sanitized upstream by sanitize_display_options).
    for passthrough in ("display", "sort", "limit"):
        value = inferred.get(passthrough) if isinstance(inferred, dict) else None
        if value is not None:
            spec[passthrough] = value

    spec, demoted = ensure_renderable(spec, column_fields(formatted))
    meta["demoted"] = demoted
    meta["source"] = "llm_refined" if meta.get("applied") else "deterministic"
    meta["type"] = spec.get("type")
    return spec, meta
