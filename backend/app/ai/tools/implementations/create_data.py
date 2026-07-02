import json
import asyncio
import logging
import re
import time as _time
from typing import AsyncIterator, Dict, Any, Type, Optional, List, Union
from pydantic import BaseModel
from app.core.otel import get_tracer
from app.ee.audit.tool_audit import log_tool_audit, _truncate_queries

tracer = get_tracer(__name__)
logger = logging.getLogger(__name__)

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    CreateDataInput,
    CreateDataOutput,
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolStdoutEvent,
    ToolEndEvent,
)
from app.ai.agents.coder.coder import Coder
from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.context.data_preview import build_data_preview, gate_stats_for_privacy
from app.ai.llm import LLM
from app.ai.llm.types import Message, TextDeltaEvent
from app.dependencies import async_session_maker
from app.services.usage_policy_service import UsageLimitContext
from app.ai.tools.schemas import DataModel
from app.ai.tools.schemas.create_data_model import normalize_group_by
from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest
from app.ai.prompt_formatters import build_codegen_context
from app.schemas.view_schema import (
    AxisOptions,
    AreaChartView,
    BarChartView,
    CountView,
    HeatmapView,
    LegendOptions,
    LineChartView,
    MetricCardView,
    Palette,
    PieChartView,
    ScatterPlotView,
    SeriesStyle,
    SparklineConfig,
    TableView,
    ViewSchema,
)


ALLOWED_VIZ_TYPES = {
    "table","bar_chart","line_chart","pie_chart","area_chart","count","metric_card",
    "heatmap","map","candlestick","treemap","radar_chart","scatter_plot",
}


def _extract_json_object(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """Best-effort extraction of a single JSON object from an LLM response.

    The visualization-inference prompt asks for "only valid JSON", but models
    routinely wrap it in ```json fences and/or append a prose rationale. A bare
    ``json.loads`` then throws, and the caller's ``except`` discards the whole
    candidate — dropping the series and the breakdown ``group_by``. This tries,
    in order: a direct parse, a parse after stripping markdown code fences, and
    finally the first balanced ``{...}`` object found in the text.
    """
    if not text:
        return None

    def _as_dict(s: str) -> Optional[Dict[str, Any]]:
        try:
            obj = json.loads(s)
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None

    # 1) Direct parse.
    obj = _as_dict(text)
    if obj is not None:
        return obj

    # 2) Strip a leading ```json / ``` fence and any closing fence, then retry.
    stripped = re.sub(r'^\s*```(?:[A-Za-z0-9_\-]+)?\s*\r?\n', '', text.strip())
    stripped = re.sub(r'(?m)^\s*```\s*$', '', stripped)
    obj = _as_dict(stripped)
    if obj is not None:
        return obj

    # 3) Scan for the first balanced top-level object (drops trailing prose).
    start = stripped.find('{')
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(stripped)):
        c = stripped[i]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return _as_dict(stripped[start:i + 1])
    return None


def _infer_palette_theme(runtime_ctx: Dict[str, Any]) -> Optional[str]:
    report_theme = runtime_ctx.get("report_theme_name")
    if report_theme:
        return str(report_theme)
    org_settings = runtime_ctx.get("settings")
    try:
        return str(org_settings.get_config("default_theme").value)
    except Exception:
        return None


_VALID_AGGREGATIONS = {"sum", "avg", "count", "min", "max"}


def _build_series_styles(series: List[Dict[str, Any]]) -> List[SeriesStyle]:
    styles: List[SeriesStyle] = []
    for entry in series or []:
        key = entry.get("value") or entry.get("name")
        if not key:
            continue
        label = entry.get("name")
        raw_agg = entry.get("aggregation")
        # Drop unknown aggregation values instead of failing construction, so a
        # bad hint doesn't erase the label/color fields for this series.
        agg = raw_agg if raw_agg in _VALID_AGGREGATIONS else None
        try:
            styles.append(SeriesStyle(key=str(key), label=label, aggregation=agg))
        except Exception:
            try:
                styles.append(SeriesStyle(key=str(key), label=label))
            except Exception:
                continue
    return styles


def _build_default_filters(data_model: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract default filters from a DataModel dict into the view shape.

    Stored as flat dicts matching DefaultFilterCondition. The runtime is
    responsible for wrapping them into a FilterGroup with the proper
    vizId:column encoding when seeding shared filters.
    """
    raw = (data_model or {}).get("filters") or []
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for f in raw:
        if not isinstance(f, dict):
            continue
        col = f.get("column") or f.get("field")
        op = f.get("operator") or f.get("op")
        if not col or not op:
            continue
        out.append({"column": str(col), "operator": str(op), "value": f.get("value")})
    return out


def _first_series_aggregation(series: List[Dict[str, Any]]) -> Optional[str]:
    """Pull an aggregation hint from the first series entry if present."""
    if not series:
        return None
    first = series[0] if isinstance(series[0], dict) else {}
    agg = first.get("aggregation")
    if agg in _VALID_AGGREGATIONS:
        return agg
    return None


# Keys carried from the inferred (viz-inference) data_model into the final one.
# The final type is forced to the user/early-requested type, but these shaping
# fields come from inference. `filters` MUST be here: the inference prompt tells
# the model to emit top-level default filters to reduce a granular/melted table
# (e.g. a `Metric | Value | Format` KPI table) down to the one relevant row.
# Dropping it makes count/metric_card render the first (unfiltered) row — the
# date or the metric label — instead of the value the user asked for.
_INFERRED_DM_CARRY_KEYS = ("series", "group_by", "sort", "limit", "filters")


def finalize_inferred_data_model(
    fallback_type: str,
    inferred_dm: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the final data_model: forced type + shaping fields from inference.

    The visualization type is pinned to the user/early-requested ``fallback_type``;
    only the series/grouping/sort/limit/filters come from the inference pass.
    """
    final_dm: Dict[str, Any] = {"type": fallback_type, "series": []}
    if isinstance(inferred_dm, dict):
        for key in _INFERRED_DM_CARRY_KEYS:
            if inferred_dm.get(key) is not None:
                final_dm[key] = inferred_dm.get(key)
    return final_dm


# Single-value card types: they render exactly one row (the frontend shows
# rows[0]), so a melted/long table must be narrowed to the asked-for row.
_SINGLE_VALUE_CARD_TYPES = {"count", "metric_card"}


def _norm(s: Any) -> str:
    return str(s).strip().lower() if s is not None else ""


_CURRENCY_SEPARATOR_RE = re.compile(r"[₪$€£¥,\s]")
_NUMERIC_STRING_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")
_DATE_STRING_RE = re.compile(
    r"^\d{4}-\d{1,2}-\d{1,2}([ T].*)?$"      # 2025-12-22 / ISO
    r"|^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$"     # 23.06.2026, 6/23/26
    r"|^\d{4}[./]\d{1,2}[./]\d{1,2}$"         # 2026.06.23
)
_TIME_COLUMN_NAME_RE = re.compile(
    r"(date|time|day|month|week|year|period|timestamp|תאריך|חודש|שנה|יום|שבוע)",
    re.IGNORECASE,
)


def _parse_numeric_like(value: Any) -> Optional[float]:
    """Parse a raw or display-formatted numeric cell.

    Accepts real numbers and strings like "1234", "₪29,134,139", "4,125.04",
    "45.2%". Returns None for anything else (dates, labels, mixed text) so
    callers can tell "numeric measure" from "label/date" reliably.
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


def _looks_like_date_string(value: Any) -> bool:
    return isinstance(value, str) and bool(_DATE_STRING_RE.match(value.strip()))


def _column_fields(formatted: Dict[str, Any]) -> List[str]:
    return [
        c.get("field")
        for c in ((formatted or {}).get("columns") or [])
        if isinstance(c, dict) and c.get("field")
    ]


def _column_cells(rows: List[Dict[str, Any]], col: str, cap: int = 50) -> List[Any]:
    out = []
    for r in rows[:cap]:
        if isinstance(r, dict) and r.get(col) is not None:
            out.append(r.get(col))
    return out


def _numeric_like_ratio(cells: List[Any]) -> float:
    if not cells:
        return 0.0
    return sum(1 for c in cells if _parse_numeric_like(c) is not None) / len(cells)


def _pick_value_column(rows: List[Dict[str, Any]], columns: List[str]) -> Optional[str]:
    """Deterministically pick the measure column when inference didn't provide one.

    Rules (strict on purpose — returning None demotes the card to a table):
    - single column → that column (a deliberate single-value result);
    - exactly one numeric-looking column → that column;
    - several numeric-looking columns → drop time/id-named ones; if one remains
      use it; for single-row results fall back to the first (mirrors the
      legacy frontend behaviour for wide KPI rows); multi-row stays ambiguous.
    """
    if not rows or not columns:
        return None
    if len(columns) == 1:
        return columns[0]
    numericish = []
    for col in columns:
        cells = _column_cells(rows, col)
        if cells and _numeric_like_ratio(cells) >= 0.5:
            numericish.append(col)
    if len(numericish) == 1:
        return numericish[0]
    if not numericish:
        return None
    deprioritized = re.compile(r"(date|year|month|week|day|time|id)$", re.IGNORECASE)
    preferred = [c for c in numericish if not deprioritized.search(str(c))]
    pool = preferred or numericish
    if len(pool) == 1:
        return pool[0]
    if len(rows) == 1:
        return pool[0]
    return None


def _match_default_filter(row: Dict[str, Any], f: Dict[str, Any]) -> bool:
    """Mirror of the frontend's matchDefaultFilter (ToolWidgetPreview.vue)."""
    if not isinstance(row, dict) or not isinstance(f, dict) or not f.get("column"):
        return True
    key = next((k for k in row.keys() if _norm(k) == _norm(f.get("column"))), None)
    if key is None:
        return True
    cell = row.get(key)
    s_cell = "" if cell is None else str(cell)
    s_val = "" if f.get("value") is None else str(f.get("value"))
    op = str(f.get("operator") or "equals")
    if op == "equals":
        return s_cell == s_val
    if op == "not_equals":
        return s_cell != s_val
    if op == "contains":
        return s_val in s_cell
    if op == "not_contains":
        return s_val not in s_cell
    if op == "starts_with":
        return s_cell.startswith(s_val)
    if op == "ends_with":
        return s_cell.endswith(s_val)
    if op == "is_empty":
        return s_cell == ""
    if op == "is_not_empty":
        return s_cell != ""
    if op in ("greater_than", "less_than", "gte", "lte"):
        a = _parse_numeric_like(cell)
        b = _parse_numeric_like(f.get("value"))
        if a is None or b is None:
            return False
        return {
            "greater_than": a > b,
            "less_than": a < b,
            "gte": a >= b,
            "lte": a <= b,
        }[op]
    return True


def _count_filter_hits(rows: List[Dict[str, Any]], filters: List[Dict[str, Any]]) -> int:
    return sum(
        1
        for r in rows
        if isinstance(r, dict) and all(_match_default_filter(r, f) for f in filters if isinstance(f, dict))
    )


def _has_time_like_column(
    formatted: Dict[str, Any],
    rows: List[Dict[str, Any]],
    columns: List[str],
    exclude: Optional[str],
) -> bool:
    """A time axis next to the measure marks the legit multi-row card pattern
    (latest value + sparkline), which must keep its legacy behaviour."""
    column_info = ((formatted or {}).get("info") or {}).get("column_info") or {}
    for col in columns:
        if col == exclude:
            continue
        if _TIME_COLUMN_NAME_RE.search(str(col)):
            return True
        dtype = str((column_info.get(col) or {}).get("dtype") or "")
        if "datetime" in dtype or dtype == "date":
            return True
        cells = _column_cells(rows, col)
        if cells and sum(1 for c in cells if _looks_like_date_string(c)) / len(cells) >= 0.8:
            return True
    return False


def ensure_single_value_card_renderable(
    final_dm: Dict[str, Any],
    formatted: Dict[str, Any],
) -> Dict[str, Any]:
    """Deterministic last line of defense for count/metric_card.

    A single-value card must be able to point at one numeric cell: a concrete
    value column, plus (for multi-row results) a row selector — a filter that
    matches exactly one row, an aggregation, or a time axis (the sparkline /
    "latest value" pattern). When none of that holds — the exact state produced
    by a melted ``Metric | Value`` display table with a failed inference pass —
    the card would render an arbitrary cell (the date row or a label), so the
    type is demoted to ``table``, which is always a truthful rendering.

    Purely structural: no LLM calls, no text matching beyond what
    ``derive_kpi_row_filter`` already does.
    """
    if not isinstance(final_dm, dict) or final_dm.get("type") not in _SINGLE_VALUE_CARD_TYPES:
        return final_dm

    rows = (formatted or {}).get("rows") or []
    if not rows:
        return final_dm  # empty result renders as '—' honestly
    columns = _column_fields(formatted)
    if not columns:
        return final_dm

    out = dict(final_dm)
    series = [dict(s) for s in (out.get("series") or []) if isinstance(s, dict)]
    first = series[0] if series else {}

    # 1) Resolve the value column; drop hallucinated ones.
    value_col = first.get("value") or first.get("metric")
    if value_col is not None and not any(_norm(value_col) == _norm(c) for c in columns):
        value_col = None
    if value_col is None:
        value_col = _pick_value_column(rows, columns)
        if value_col is not None:
            first = {**first, "value": value_col}
            series = [first] + series[1:] if series else [first]
            out["series"] = series
    if value_col is None:
        return _demote_card_to_table(out, reason="no resolvable value column")

    if len(rows) == 1:
        return out  # rows[0] is the answer and the column is pinned

    # 2) Multi-row: an existing filter must select exactly one row.
    filters = [f for f in (out.get("filters") or []) if isinstance(f, dict)]
    if filters:
        if _count_filter_hits(rows, filters) == 1:
            return out
        out["filters"] = None  # invalid filter — drop rather than mislead

    # 3) Melted-table row selection by series-name match (existing safeguard).
    derived = derive_kpi_row_filter(out, formatted)
    if derived:
        out["filters"] = [derived]
        return out

    # 4) Aggregation over a numeric measure is a valid row-reducer — unless the
    #    measure column mixes in date strings (melted-table symptom: summing a
    #    date together with the metrics would show garbage).
    agg = first.get("aggregation")
    if agg in _VALID_AGGREGATIONS:
        cells = _column_cells(rows, value_col)
        mixed_with_dates = any(_looks_like_date_string(c) for c in cells)
        if agg == "count" or (_numeric_like_ratio(cells) >= 0.5 and not mixed_with_dates):
            return out

    # 5) Time series next to the measure: legacy "latest value (+sparkline)" card.
    if (
        first.get("sparkline_column")
        or first.get("time_series")
        or out.get("has_time_series")
        or _has_time_like_column(formatted, rows, columns, exclude=value_col)
    ):
        return out

    return _demote_card_to_table(out, reason="multi-row result with no row selector")


def _demote_card_to_table(final_dm: Dict[str, Any], reason: str) -> Dict[str, Any]:
    logger.info(
        "create_data: demoting %s to table (%s)", final_dm.get("type"), reason
    )
    demoted = dict(final_dm)
    demoted["type"] = "table"
    demoted["filters"] = None
    return demoted


def derive_kpi_row_filter(
    final_dm: Dict[str, Any],
    formatted: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Derive a row-selecting filter for a single-value card over a melted table.

    A ``count``/``metric_card`` shows one row. When the underlying result is a
    *melted / long* KPI table — a label column plus a single shared value column,
    one row per metric (``Metric | Value | Format``) — picking ``value="Value"``
    is ambiguous: the frontend falls back to ``rows[0]`` (the date/first metric)
    unless a filter narrows the data to the asked-for metric.

    The viz-inference prompt asks the model to emit that filter, but it does so
    only sometimes (it may emit an ``aggregation`` instead, which the card
    ignores). This is the deterministic safeguard: if no filter is present, match
    the series' display name against the cells of a non-value column and, on a
    hit, return a filter selecting that row. Conservative — returns ``None``
    unless there is a clear single match, so normal results are untouched.
    """
    if (final_dm or {}).get("type") not in _SINGLE_VALUE_CARD_TYPES:
        return None
    if final_dm.get("filters"):
        return None

    rows = (formatted or {}).get("rows") or []
    if len(rows) < 2:  # one row: rows[0] is already the answer
        return None

    series = final_dm.get("series") or []
    first = series[0] if series and isinstance(series[0], dict) else {}
    value_col = first.get("value")
    label = first.get("name")
    if not value_col or not label:
        return None

    columns = [
        c.get("field")
        for c in (formatted.get("columns") or [])
        if isinstance(c, dict) and c.get("field")
    ]
    label_norm = _norm(label)

    # Prefer an exact cell match; fall back to a substring match (the series name
    # is often a shortened form of the label cell, e.g. "Revenue" vs
    # "Total Revenue (Revenue)"). Require a single matching row to stay safe.
    for matcher in (
        lambda cell: _norm(cell) == label_norm,
        lambda cell: len(label_norm) >= 3 and (label_norm in _norm(cell) or _norm(cell) in label_norm),
    ):
        for col in columns:
            if col == value_col:
                continue
            hits = [r for r in rows if isinstance(r, dict) and r.get(col) is not None and matcher(r.get(col))]
            if len(hits) == 1:
                return {"column": col, "operator": "equals", "value": hits[0].get(col)}
    return None


def build_view_from_data_model(
    data_model: Dict[str, Any],
    title: Optional[str] = None,
    palette_theme: Optional[str] = None,
    available_columns: Optional[List[str]] = None,
) -> Optional[ViewSchema]:
    try:
        chart_type = str((data_model or {}).get("type") or "").lower()
    except Exception:
        return None

    palette = Palette(theme=(palette_theme or "default"))
    series = data_model.get("series") or []
    default_filters = _build_default_filters(data_model)

    if chart_type in {"bar_chart", "line_chart", "area_chart"}:
        x_key = next((s.get("key") for s in series if s.get("key")), None)
        value_cols = [s.get("value") for s in series if s.get("value")]

        # Fallback: infer x_key from available columns when missing
        # Pick the first column that's not used as a value
        if not x_key and value_cols and available_columns:
            value_cols_set = set(value_cols)
            x_key = next((col for col in available_columns if col not in value_cols_set), None)

        if not x_key or not value_cols:
            return None
        # Use list when multiple measures exist
        y_value: Union[str, List[str]] = value_cols[0] if len(value_cols) == 1 else value_cols
        series_styles = _build_series_styles(series)
        # group_by may arrive as a string (planner) or a list (other tools);
        # the view expects a single column name.
        group_by = normalize_group_by(data_model.get("group_by"))
        # Show legend if multiple series or groupBy is used
        has_multiple_series = len(series) > 1 or bool(group_by)
        view_cls = {
            "bar_chart": BarChartView,
            "line_chart": LineChartView,
            "area_chart": AreaChartView,
        }.get(chart_type, BarChartView)
        view = view_cls(
            title=title,
            x=str(x_key),
            y=y_value,
            groupBy=group_by,
            palette=palette,
            seriesStyles=series_styles,
            legend=LegendOptions(show=bool(has_multiple_series)),
            defaultFilters=default_filters,
        )
        # Slightly different axis defaults for time series vs categorical
        view.axisX = AxisOptions(rotate=45, interval=0)
        view.axisY = AxisOptions(show=True, rotate=0, interval=0)
        return ViewSchema(view=view)

    if chart_type == "pie_chart":
        base = series[0] if series else {}
        category = base.get("key")
        value = base.get("value")
        if not category or not value:
            return None
        view = PieChartView(
            title=title,
            category=str(category),
            value=str(value),
            palette=palette,
            legend=LegendOptions(show=True, position="right"),  # Pie charts benefit from legend
            aggregation=_first_series_aggregation(series),
            defaultFilters=default_filters,
        )
        return ViewSchema(view=view)

    if chart_type == "scatter_plot":
        base = series[0] if series else {}
        x_key = base.get("x") or base.get("key")
        y_key = base.get("y") or base.get("value")
        if not x_key or not y_key:
            return None
        view = ScatterPlotView(
            title=title,
            x=str(x_key),
            y=str(y_key),
            size=base.get("size"),
            colorBy=base.get("color"),
            palette=palette,
            aggregation=_first_series_aggregation(series),
            defaultFilters=default_filters,
        )
        return ViewSchema(view=view)

    if chart_type == "heatmap":
        base = series[0] if series else {}
        x_key = base.get("x") or base.get("key")
        y_key = base.get("y")
        value_key = base.get("value")
        if not x_key or not y_key or not value_key:
            return None
        # Determine color scheme from series config or default to blue
        color_scheme = base.get("colorScheme") or base.get("color_scheme") or "blue"
        if color_scheme not in ("blue", "green", "red", "violet", "orange"):
            color_scheme = "blue"
        # Check if values should be shown (default True)
        show_values = base.get("showValues", True)
        if show_values is None:
            show_values = True
        view = HeatmapView(
            title=title,
            x=str(x_key),
            y=str(y_key),
            value=str(value_key),
            colorScheme=color_scheme,
            showValues=bool(show_values),
            axisX=AxisOptions(rotate=45, interval=0),
            axisY=AxisOptions(rotate=0, interval=0),
            aggregation=_first_series_aggregation(series),
            defaultFilters=default_filters,
        )
        return ViewSchema(view=view)

    if chart_type == "table":
        view = TableView(title=title, defaultFilters=default_filters)
        return ViewSchema(view=view)

    # CountView - simple single value display (value is optional)
    if chart_type == "count":
        base = series[0] if series else {}
        value_key = base.get("value") or base.get("metric") or base.get("key") or base.get("name")
        view = CountView(
            title=title,
            value=str(value_key) if value_key else None,
            palette=palette,
            aggregation=_first_series_aggregation(series),
            defaultFilters=default_filters,
        )
        return ViewSchema(view=view)

    # MetricCardView - richer KPI card with sparkline/trend support
    if chart_type == "metric_card":
        base = series[0] if series else {}
        value_key = base.get("value") or base.get("metric")
        # For metric_card, value is required; fallback gracefully
        if not value_key:
            # Try to use first available column name from series
            value_key = base.get("key") or base.get("name")

        # Extract comparison/trend column
        comparison_key = base.get("comparison") or base.get("trend") or base.get("change")

        # Build sparkline config if LLM specified time-series columns
        sparkline = None
        sparkline_col = base.get("sparkline_column") or base.get("time_series")
        sparkline_x = base.get("sparkline_x") or base.get("date") or base.get("time")

        # Only enable sparkline if LLM explicitly configured it
        if sparkline_col or data_model.get("has_time_series"):
            sparkline = SparklineConfig(
                enabled=True,
                column=sparkline_col or value_key,
                xColumn=sparkline_x,
                type="area",
            )

        # Determine if trend should be inverted (down is good)
        # Use `or False` because base.get returns None if key exists with None value
        invert_trend = base.get("invert_trend") or False
        comparison_label = base.get("comparison_label") or base.get("trend_label")

        # value is REQUIRED for MetricCardView - if we don't have it, fall back to CountView
        if not value_key:
            view = CountView(title=title, palette=palette, defaultFilters=default_filters)
            return ViewSchema(view=view)

        view = MetricCardView(
            title=title,
            value=str(value_key),
            comparison=str(comparison_key) if comparison_key else None,
            comparisonLabel=comparison_label,
            invertTrend=invert_trend,
            sparkline=sparkline,
            palette=palette,
            aggregation=_first_series_aggregation(series),
            defaultFilters=default_filters,
        )
        return ViewSchema(view=view)

    return None


class CreateDataTool(Tool):
    # --- Visualization inference (post-execution) ---------------------------------------------
    @staticmethod
    def _build_viz_profile(formatted: Dict[str, Any], allow_llm_see_data: bool) -> Dict[str, Any]:
        info = formatted.get("info", {}) if isinstance(formatted, dict) else {}
        column_info = info.get("column_info") or {}
        cols = []
        for name, meta in (column_info.items() if isinstance(column_info, dict) else []):
            col = {
                "name": name,
                "dtype": meta.get("dtype"),
                "non_null_count": meta.get("non_null_count"),
                "unique_count": meta.get("unique_count"),
                "null_count": meta.get("null_count"),
            }
            # min/max are verbatim cell values. Expose them only when the LLM may
            # see data, except date/time ranges which stay as low-sensitivity
            # metadata useful for axis scaling.
            _dtype = str(meta.get("dtype", ""))
            if allow_llm_see_data or "datetime" in _dtype or "date" in _dtype:
                col["min"] = meta.get("min")
                col["max"] = meta.get("max")
            cols.append(col)
        profile: Dict[str, Any] = {
            "row_count": info.get("total_rows"),
            "column_count": info.get("total_columns"),
            "columns": cols,
        }
        if allow_llm_see_data:
            # Add a tiny head sample for better inference (privacy-aware)
            profile["head_rows"] = (formatted.get("rows") or [])[:5]
        return profile

    async def _infer_visualization_model(
        self,
        runtime_ctx: Dict[str, Any],
        user_prompt: str,
        messages_context: str,
        formatted: Dict[str, Any],
        allow_llm_see_data: bool,
    ) -> Dict[str, Any]:
        """Ask a small LLM pass to pick visualization type and series from schema/stats (+sample).

        Returns a minimal DataModel dict validated against schema: at least { type, series? }.
        Fallback to {"type": "table", "series": []} on failure.
        """
        with tracer.start_as_current_span("create_data.infer_visualization") as span:
            return await self._infer_visualization_model_traced(span, runtime_ctx, user_prompt, messages_context, formatted, allow_llm_see_data)

    async def _infer_visualization_model_traced(self, span, runtime_ctx, user_prompt, messages_context, formatted, allow_llm_see_data):
        info = formatted.get("info", {}) if isinstance(formatted, dict) else {}
        span.set_attribute("data.row_count", info.get("total_rows", 0) or 0)
        span.set_attribute("data.column_count", info.get("total_columns", 0) or 0)
        base_usage_ctx = runtime_ctx.get("usage_limit_context")
        usage_ctx = (
            base_usage_ctx.for_source("create_data.viz_infer", runtime_ctx.get("tool_call_id"))
            if isinstance(base_usage_ctx, UsageLimitContext)
            else None
        )
        llm = LLM(runtime_ctx.get("model"), usage_session_maker=async_session_maker, usage_context=usage_ctx)
        profile = self._build_viz_profile(formatted, allow_llm_see_data)

        # Fetch visualization-specific instructions
        viz_instructions = ""
        context_hub = runtime_ctx.get("context_hub")
        if context_hub and getattr(context_hub, "instruction_builder", None):
            try:
                viz_section = await context_hub.instruction_builder.build(categories=["visualizations", "visualization", "general"])
                viz_instructions = viz_section.render() or ""
            except Exception:
                viz_instructions = ""

        allowed_types = list(ALLOWED_VIZ_TYPES)

        # Build column names list for reference
        column_names = [c.get("name", "") for c in profile.get("columns", [])]
        row_count = profile.get("row_count", 0)
        
        # Build instructions block for prompt
        instructions_block = ""
        if viz_instructions:
            instructions_block = f"""
ORGANIZATION VISUALIZATION INSTRUCTIONS:
{viz_instructions}

"""
        
        prompt = f"""Role: visualization planner. Analyze the data profile and choose the best visualization type.
{instructions_block}
Use the exact column names from the data. Available columns are: {column_names}

Context: {messages_context or "None"}
User prompt: {user_prompt or "None"}

Data profile:
{json.dumps(profile, ensure_ascii=False, indent=2)}

═══════════════════════════════════════════════════════════════════════════════
RULES FOR METRIC_CARD (KPI display)
═══════════════════════════════════════════════════════════════════════════════

Use metric_card when showing a single key metric. The "value" field should be an exact column name.

Detecting the value column:
- Look for columns with names like: revenue, total, amount, count, sum, value, sales, profit, cost
- Avoid using date/time columns (year, month, date, week, day) as the value
- Avoid using ID columns as the value
- Pick the column that represents the metric the user asked about

DETECTING TIME-SERIES FOR SPARKLINE:
If row_count > 1 AND there's a time column (month, date, week, year, period, day), enable sparkline:
- sparkline_column: same as value column (the metric to plot over time)
- sparkline_x: the time column (month, date, etc.)

EXAMPLE 1 - Monthly revenue data (7 rows):
Columns: ["year", "month", "revenue"]
CORRECT:
{{"type": "metric_card", "series": [{{"name": "Revenue", "value": "revenue", "sparkline_column": "revenue", "sparkline_x": "month"}}]}}

WRONG (uses generic "value" instead of actual column name):
{{"type": "metric_card", "series": [{{"name": "Revenue", "value": "value"}}]}}

EXAMPLE 2 - Single total row:
Columns: ["total_sales"]
CORRECT:
{{"type": "metric_card", "series": [{{"name": "Total Sales", "value": "total_sales"}}]}}

EXAMPLE 3 - Revenue with comparison:
Columns: ["current_revenue", "change_pct"]
CORRECT:
{{"type": "metric_card", "series": [{{"name": "Revenue", "value": "current_revenue", "comparison": "change_pct"}}]}}

═══════════════════════════════════════════════════════════════════════════════
OTHER CHART TYPES
═══════════════════════════════════════════════════════════════════════════════

Allowed types: {", ".join(allowed_types)}

Series contracts:
- bar/line/area: [{{"name", "key", "value", "aggregation?"}}] — both `key` and `value` are required.
- pie/map: [{{"name", "key", "value", "aggregation?"}}]
- scatter: [{{"name", "x", "y", "aggregation?"}}] (+ size optional)
- heatmap: [{{"name", "x", "y", "value", "colorScheme", "showValues", "aggregation?"}}]
  - colorScheme: "blue" | "green" | "red" | "violet" | "orange" (default: "blue")
  - showValues: true | false (default: true) — whether to show values in cells
- table: series: []

For bar/line/area charts:
- "key" = the category column (x-axis), required — usually a date, name, or category column
- "value" = the numeric column (y-axis), required — the metric to display
- Include both "key" and "value" in every series entry.

DETECTING GROUP_BY (for multi-series grouped bar/line/area charts):
- If the data has a CATEGORICAL column that creates MULTIPLE ROWS per x-axis value, use "group_by"
- Look at unique_count in the data profile: if a column has few unique values (2-10) that repeat across x-axis categories, it's likely a grouping column
- Common group_by column names: category, type, group, segment, channel, region, product, source, status
- When group_by is used, each unique value in that column becomes a separate series (colored bar/line)

EXAMPLE 1 - Simple bar chart (one value per x-axis category):
Columns: ["date", "max_bitcoin_price"]
CORRECT:
{{"type": "bar_chart", "series": [{{"name": "Max Bitcoin Price", "key": "date", "value": "max_bitcoin_price"}}]}}

EXAMPLE 2 - Grouped bar chart (multiple categories per x-axis value):
Columns: ["month", "revenue_group", "revenue"]
Data pattern: Each month has multiple rows (one per revenue_group: CARDS, FX, SAAS, etc.)
CORRECT (with group_by):
{{"type": "bar_chart", "series": [{{"name": "Revenue", "key": "month", "value": "revenue"}}], "group_by": "revenue_group"}}

WRONG (missing group_by - all bars will show same value!):
{{"type": "bar_chart", "series": [{{"name": "Revenue", "key": "month", "value": "revenue"}}]}}

EXAMPLE 3 - Line chart with multiple series by category:
Columns: ["date", "channel", "sales"]
Data pattern: Each date has rows for different channels (online, retail, wholesale)
CORRECT:
{{"type": "line_chart", "series": [{{"name": "Sales", "key": "date", "value": "sales"}}], "group_by": "channel"}}

WRONG (missing key - will break the chart):
{{"type": "bar_chart", "series": [{{"name": "Max Bitcoin Price", "value": "max_bitcoin_price"}}]}}

HEATMAP EXAMPLE:
Columns: ["day_of_week", "hour", "activity_count"]
Data pattern: Each combination of day_of_week and hour has a value
CORRECT:
{{"type": "heatmap", "series": [{{"name": "Activity", "x": "hour", "y": "day_of_week", "value": "activity_count", "colorScheme": "blue", "showValues": true}}]}}

DECISION LOGIC:
1. Single numeric value → metric_card
2. Multiple rows with time column + numeric value → metric_card WITH sparkline
3. Category + values → bar_chart or pie_chart
4. Two numeric columns → scatter_plot
5. Time series for trends → line_chart or area_chart
6. Two categorical columns + numeric value (matrix/grid data) → heatmap
7. Raw data display → table

═══════════════════════════════════════════════════════════════════════════════
Granularity: aggregation and default filters
═══════════════════════════════════════════════════════════════════════════════

Data is often granular — many rows per x-axis category or per metric value. Pick
an "aggregation" on each series or emit top-level "filters" to reduce the rows
to one per bucket.

Detecting granularity:
- Compute expected_rows = unique_count(chosen_key) × unique_count(group_by or 1).
- If row_count exceeds expected_rows, the data has multiple rows per bucket —
  pick an aggregation or a filter. Without one the chart shows only the first
  row per bucket, which is usually wrong.

Aggregation values: "sum" | "avg" | "count" | "min" | "max"
- sum: totals (revenue, amount, qty) — the common default
- avg: averages (price, score, rating)
- count: row counts (transactions, events) — rarely the `value` column itself
- min/max: extrema (latest price, highest score)

Aggregation example (cartesian, granular transactions):
Columns: ["transaction_date", "amount", "region"]
row_count: 5,000; unique_count(transaction_date): 30; unique_count(region): 4
Expected rows without aggregation: 30 × 4 = 120, but the profile shows 5,000 — granular.
Recommended (aggregate sum per date+region):
{{"type": "bar_chart", "series": [{{"name": "Revenue", "key": "transaction_date", "value": "amount", "aggregation": "sum"}}], "group_by": "region"}}

Aggregation example (metric_card, granular daily sales):
Columns: ["date", "sales"]
row_count: 365; unique_count(date): 365
Without aggregation, metric_card shows the first row's sales only (not a KPI).
Recommended:
{{"type": "metric_card", "series": [{{"name": "Total Sales", "value": "sales", "aggregation": "sum"}}]}}

Default filters (alternative to aggregation):
Use "filters" at the top level to reduce granular data down to a single row per
bucket when the user's intent is clearly "just the latest" or "just this one
segment". Filters open the widget pre-filtered and remain user-editable.

Filter shape: [{{"column": "<column>", "operator": "<op>", "value": <value>}}]
Operators: "equals", "not_equals", "contains", "not_contains", "starts_with",
"ends_with", "greater_than", "less_than", "gte", "lte", "before", "after",
"is_empty", "is_not_empty".

Default filters example (pick latest period):
Columns: ["month", "revenue"]
User prompt: "show this month's revenue"
{{"type": "metric_card", "series": [{{"name": "Revenue", "value": "revenue"}}],
  "filters": [{{"column": "month", "operator": "equals", "value": "2024-06"}}]}}

Prefer aggregation when the intent is "all data, summarized". Prefer filters
when the intent is "this specific slice". Setting both is rarely useful.

═══════════════════════════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════════════════════════

Return only valid JSON:
{{"type": "...", "series": [...], "group_by": "column_name_or_null", "filters": [...]}}

Include "group_by" when the data has multiple rows per x-axis category that should be shown as separate colored series.
Include "aggregation" on each series entry when rows are granular.
Include "filters" only when narrowing the data to a specific slice.

Reminder: use exact column names from: {column_names}
Do not use generic placeholders like "value" unless that is the actual column name."""

        _viz_t0 = _time.perf_counter()
        raw = None
        try:
            chunks: list[str] = []
            async for evt in llm.inference_stream_v2(
                messages=[Message(role="user", content=prompt)],
                usage_scope="create_data.viz_infer",
            ):
                if isinstance(evt, TextDeltaEvent):
                    chunks.append(evt.text)
            raw = "".join(chunks) or None
        except Exception:
            raw = None
        finally:
            logger.info(
                "create_data.viz_infer elapsed_ms=%.0f got_raw=%s",
                (_time.perf_counter() - _viz_t0) * 1000.0,
                raw is not None,
            )

        candidate = {"type": "table", "series": []}
        view_options: Dict[str, Any] | None = None
        if raw:
            # Models routinely wrap the JSON in ```json fences and append a prose
            # "Rationale" section despite the "return only JSON" instruction, so a
            # bare json.loads(raw) throws and the breakdown is silently lost.
            # Extract the first balanced JSON object instead.
            candidate_json = _extract_json_object(raw)
            if isinstance(candidate_json, dict):
                try:
                    dm = DataModel(**{k: v for k, v in candidate_json.items() if k in {"type", "series", "group_by", "sort", "limit", "filters"}})
                    candidate = dm.model_dump()
                except Exception:
                    candidate = {"type": "table", "series": []}
                # Extract optional view mappings (limit/sort/colors) from candidate_json.view
                try:
                    view = candidate_json.get("view") if isinstance(candidate_json, dict) else None
                    if isinstance(view, dict):
                        # limit
                        if view.get("limit") is not None and candidate.get("limit") is None:
                            candidate["limit"] = view.get("limit")
                        # sort { by, order }
                        sort = view.get("sort")
                        if isinstance(sort, dict) and not candidate.get("sort"):
                            by = sort.get("by") or sort.get("field")
                            order = str(sort.get("order") or "asc").lower()
                            if by:
                                candidate["sort"] = [{"field": by, "direction": ("desc" if order.startswith("d") else "asc")}]
                        # colors → view.options.colors
                        colors = None
                        if isinstance(view.get("colors"), list):
                            colors = view.get("colors")
                        elif isinstance(view.get("color"), str):
                            colors = [view.get("color")]
                        if colors:
                            view_options = {"colors": colors}
                except Exception:
                    pass

        # Normalize: ensure series exists for non-table types
        if candidate.get("type") != "table" and not candidate.get("series"):
            candidate["series"] = []
        span.set_attribute("viz.inferred_type", candidate.get("type", "table"))

        # Emit a progress event for UI when series/type are inferred
        try:
            chart_type = candidate.get("type")
            if chart_type and chart_type != "table":
                await asyncio.sleep(0)  # keep cooperative
                payload = {
                    "stage": "series_configured",
                    "series": candidate.get("series") or [],
                    "chart_type": chart_type,
                    "timing": False,
                }
                if view_options:
                    payload["view"] = {"type": chart_type, "options": view_options}
                yield_event = ToolProgressEvent(
                    type="tool.progress",
                    payload=payload,
                )
                # Use synchronous yield pattern by returning a marker to the caller
                return {"data_model": candidate, "progress_event": yield_event, "view_options": view_options}
        except Exception:
            pass
        return {"data_model": candidate, "progress_event": None, "view_options": view_options}
    @staticmethod
    async def _build_schemas_excerpt(context_hub, context_view, user_text: str, top_k: int = 10) -> str:
        """Best-effort schema excerpt similar to CreateWidgetTool, with keyword fallback."""
        try:
            import re
            if context_hub and getattr(context_hub, "schema_builder", None):
                tokens = [t.lower() for t in re.findall(r"[a-zA-Z0-9_]{3,}", user_text or "")]
                seen = set()
                keywords = []
                for t in tokens:
                    if t in seen:
                        continue
                    seen.add(t)
                    keywords.append(t)
                    if len(keywords) >= 3:
                        break
                name_patterns = [f"(?i){re.escape(k)}" for k in keywords] if keywords else None

                _t0 = _time.perf_counter()
                ctx = await context_hub.schema_builder.build(
                    with_stats=True,
                    name_patterns=name_patterns,
                )
                logger.info(
                    "create_data.schema_build stage=fallback_excerpt elapsed_ms=%.0f patterns=%d",
                    (_time.perf_counter() - _t0) * 1000.0,
                    len(name_patterns or []),
                )
                return ctx.render_combined(top_k_per_ds=top_k, index_limit=0, include_index=False)
            _schemas_section_obj = getattr(context_view.static, "schemas", None) if context_view else None
            return _schemas_section_obj.render("gist") if _schemas_section_obj else ""
        except Exception:
            _schemas_section_obj = getattr(context_view.static, "schemas", None) if context_view else None
            return _schemas_section_obj.render() if _schemas_section_obj else ""

    @staticmethod
    async def _resolve_active_tables(
        tables_by_source: List[Any],
        schema_builder,
        data_sources: Optional[List[Any]] = None,
    ) -> tuple[List[Dict[str, Any]], List[str]]:
        """Resolve table patterns to active tables only.

        Args:
            tables_by_source: List of TablesBySource with table names/patterns
            schema_builder: SchemaContextBuilder instance
            data_sources: Optional list of data sources to get all ds_ids

        Returns:
            (resolved_tables_by_source, warnings) where:
            - resolved_tables_by_source: List of dicts with resolved active table names
            - warnings: List of warning messages for patterns with no matches
        """
        import re

        with tracer.start_as_current_span("create_data.resolve_active_tables") as span:
            span.set_attribute("tables_by_source.count", len(tables_by_source or []))

            if not tables_by_source or not schema_builder:
                return [], ["No tables_by_source or schema_builder provided"]

            resolved: List[Dict[str, Any]] = []
            warnings: List[str] = []

            for group in tables_by_source:
                ds_id = str(group.data_source_id) if getattr(group, "data_source_id", None) else None
                input_tables = getattr(group, "tables", []) or []

                if not input_tables:
                    continue

                # Build name_patterns from table names (always escaped as literal)
                name_patterns: List[str] = []
                for name in input_tables:
                    if not isinstance(name, str) or not name.strip():
                        continue
                    name = name.strip()
                    # Always escape - table names are concrete references, not regex patterns
                    esc = re.escape(name)
                    name_patterns.append(f"(?i)(?:^|[./]){esc}$")

                if not name_patterns:
                    continue

                # Resolve via schema_builder (only returns active tables)
                try:
                    _t0 = _time.perf_counter()
                    ctx = await schema_builder.build(
                        with_stats=False,
                        data_source_ids=[ds_id] if ds_id else None,
                        name_patterns=name_patterns,
                    )
                    logger.info(
                        "create_data.schema_build stage=resolve_active ds_id=%s elapsed_ms=%.0f patterns=%d",
                        ds_id,
                        (_time.perf_counter() - _t0) * 1000.0,
                        len(name_patterns),
                    )

                    # Extract resolved table names per data source
                    matched_by_ds: Dict[str, List[str]] = {}
                    for ds in (getattr(ctx, "data_sources", []) or []):
                        ds_info = getattr(ds, "info", None)
                        resolved_ds_id = getattr(ds_info, "id", None) if ds_info else None
                        for t in (getattr(ds, "tables", []) or []):
                            tbl_name = getattr(t, "name", None)
                            if tbl_name:
                                key = str(resolved_ds_id) if resolved_ds_id else "__all__"
                                matched_by_ds.setdefault(key, []).append(tbl_name)

                    # Build resolved group(s)
                    if ds_id:
                        # Scoped to specific ds_id
                        matched = matched_by_ds.get(ds_id, [])
                        if matched:
                            resolved.append({"data_source_id": ds_id, "tables": matched})
                        else:
                            warnings.append(f"No active tables matched patterns {input_tables} in data source {ds_id}")
                    else:
                        # Cross-source: create one group per ds that had matches
                        any_match = False
                        for resolved_ds_id, matched in matched_by_ds.items():
                            if matched:
                                any_match = True
                                actual_ds_id = None if resolved_ds_id == "__all__" else resolved_ds_id
                                resolved.append({"data_source_id": actual_ds_id, "tables": matched})
                        if not any_match:
                            warnings.append(f"No active tables matched patterns {input_tables} across any data source")

                except Exception as e:
                    warnings.append(f"Failed to resolve tables {input_tables}: {str(e)}")

            span.set_attribute("tables.resolved_count", sum(len(g.get("tables", [])) for g in resolved))
            return resolved, warnings

    @staticmethod
    def _summarize_errors(errors) -> dict:
        """Summarize retry errors for the planner observation.

        Keeps the DB/driver error detail that usually sits on lines 2+ of a
        Python traceback (DuckDB "Binder Error: column X not found", pyodbc
        "[42S22] Invalid column name", etc.) instead of truncating to the first
        line. Traceback frames (`  File "..."`) are dropped — they're noise for
        the planner but the underlying exception text is retained.
        """
        last_text = (errors[-1][1] if errors else "") or ""
        # Keep non-empty lines, drop `File "..."` frame lines.
        cleaned_lines = [
            ln for ln in last_text.splitlines()
            if ln.strip() and not ln.lstrip().startswith('File "')
        ]
        # Primary message: first non-frame line (usually "Execution error: ...").
        summary_line = cleaned_lines[0][:500] if cleaned_lines else ""
        # Full cleaned detail for the planner to reason about.
        detail = "\n".join(cleaned_lines)[:1500]
        payload = {
            "retry_summary": {
                "attempts": int(len(errors or [])),
                "succeeded": False,
                "error_count": int(len(errors or [])),
                "last_error_message": summary_line or detail[:300],
            }
        }
        if summary_line or detail:
            payload["error_detail"] = detail or summary_line
            payload["error_message"] = summary_line or detail[:300]
        return payload

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_data",
            description="Generate code from prompt and execute to return data resultas table or chart. Use this when you want to generate a tracked insight, or you have enough information to generate a widget. Call create_data for 1 insight at a time. If you need to generate multiple insights, call create_data multiple times. Reuse over rebuild: when the data already exists in a prior step from this report (see <available_steps>) or a published entity (see <entities>) — especially when the user refers to it by name or asks to extend/modify a previous result — prefer create_data here, which loads that data via load_step/load_entity instead of writing SQL from scratch. Queries are subject to a per-connection timeout.",
            category="action",
            version="1.0.0",
            input_schema=CreateDataInput.model_json_schema(),
            output_schema=CreateDataOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=180,
            idempotent=False,
            required_permissions=[],
            tags=["data", "code", "execution"],
            allowed_modes=["chat", "deep", "training"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreateDataInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreateDataOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        with tracer.start_as_current_span("create_data.run_stream") as run_span:
            run_span.set_attribute("tool.title", (tool_input or {}).get("title", ""))
            async for event in self._run_stream_traced(run_span, tool_input, runtime_ctx):
                yield event

    async def _run_stream_traced(self, run_span, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = CreateDataInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": data.title})
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "init"})

        # Context and views
        organization_settings = runtime_ctx.get("settings")
        context_view = runtime_ctx.get("context_view")
        context_hub = runtime_ctx.get("context_hub")

        # Early: signal intended artifact type and request step creation before code-gen
        early_viz_type = None
        try:
            # Single signal: declare type and pass the intended query title
            allowed_types = ALLOWED_VIZ_TYPES
            requested_type = None
            try:
                requested_type = str((tool_input or {}).get("visualization_type") or "").strip()
            except Exception:
                requested_type = None
            viz_type = requested_type if requested_type in allowed_types else "table"
            early_viz_type = viz_type
            yield ToolProgressEvent(
                type="tool.progress",
                payload={
                    "stage": "data_model_type_determined",
                    "data_model_type": viz_type,
                    "query_title": data.title,
                    "timing": False,
                },
            )
        except Exception:
            # Best-effort only; if creation fails now, later stages may still create
            pass

        # Determine data sources: tables and/or files
        resolved_tables: List[Dict[str, Any]] = []
        resolution_warnings: List[str] = []
        schemas_excerpt = ""
        
        # Get available files from context
        excel_files = runtime_ctx.get("excel_files", [])
        has_tables_request = bool(data.tables_by_source)
        has_files = bool(excel_files)
        
        # Resolve tables only if tables_by_source is provided
        if has_tables_request:
            if not context_hub or not getattr(context_hub, "schema_builder", None):
                # Only fail on missing schema_builder if tables were requested and no files available
                if not has_files:
                    await log_tool_audit(
                        runtime_ctx,
                        action="tool.data_query_failed",
                        resource_type="report",
                        resource_id=str(runtime_ctx.get("report").id) if runtime_ctx.get("report") else None,
                        details={
                            "tool": "create_data",
                            "error_type": "configuration_error",
                            "error_message": "Schema builder not available in context",
                        },
                    )
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": {
                                "success": False,
                                "code": "",
                                "data": {},
                                "data_preview": {},
                                "stats": {},
                                "execution_log": "",
                                "errors": [],
                            },
                            "observation": {
                                "summary": "Table resolution failed - no schema builder available",
                                "error": {
                                    "type": "configuration_error",
                                    "message": "Schema builder not available in context",
                                },
                            },
                        },
                    )
                    return
                # If files exist, proceed without tables
            else:
                yield ToolProgressEvent(type="tool.progress", payload={"stage": "resolving_tables"})
                resolved_tables, resolution_warnings = await self._resolve_active_tables(
                    data.tables_by_source,
                    context_hub.schema_builder,
                )
        
        # Check if we have any data sources (tables or files)
        total_resolved = sum(len(g.get("tables", [])) for g in resolved_tables)

        # When `enable_web_fetch` is on, the sandbox exposes `http` to the
        # coder — a URL-fetch task is a valid "no tables, no files" case.
        web_fetch_enabled = False
        try:
            _ef = organization_settings.get_config("enable_web_fetch") if organization_settings else None
            web_fetch_enabled = bool(getattr(_ef, "value", False))
        except Exception:
            web_fetch_enabled = False

        if total_resolved == 0 and not has_files and not web_fetch_enabled:
            # No tables resolved AND no files available - fail
            _requested = [
                {"data_source_id": str(g.data_source_id), "tables": g.tables}
                for g in (data.tables_by_source or [])
            ] if data.tables_by_source else []
            await log_tool_audit(
                runtime_ctx,
                action="tool.table_resolution_failed",
                resource_type="report",
                resource_id=str(runtime_ctx.get("report").id) if runtime_ctx.get("report") else None,
                details={
                    "tool": "create_data",
                    "requested_tables": _requested,
                    "warnings": resolution_warnings,
                },
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {
                        "success": False,
                        "code": "",
                        "data": {},
                        "data_preview": {},
                        "stats": {},
                        "execution_log": "",
                        "errors": [],
                    },
                    "observation": {
                        "summary": "No data sources available - no tables matched and no files uploaded",
                        "error": {
                            "type": "no_data_sources",
                            "message": "No active tables matched the requested patterns and no files are available. Either provide valid table names in tables_by_source or upload files.",
                            "warnings": resolution_warnings,
                            "requested_tables": [
                                {"data_source_id": g.data_source_id, "tables": g.tables}
                                for g in (data.tables_by_source or [])
                            ] if data.tables_by_source else [],
                        },
                    },
                },
            )
            return
        
        # Log the mode we're operating in
        if total_resolved > 0 and has_files:
            mode = "tables_and_files"
        elif total_resolved > 0:
            mode = "tables_only"
        else:
            mode = "files_only"
        
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "data_sources_resolved", "mode": mode, "tables_count": total_resolved, "files_count": len(excel_files)})
        
        # Build schemas excerpt using resolved active tables (skip if file-only mode)
        if total_resolved > 0:
            try:
                # Collect all resolved table names for schema building
                all_resolved_names: List[str] = []
                ds_ids: List[str] = []
                for group in resolved_tables:
                    if group.get("data_source_id"):
                        ds_ids.append(group["data_source_id"])
                    all_resolved_names.extend(group.get("tables", []))
                
                ds_scope = list(set(ds_ids)) if ds_ids else None
                # Use exact name patterns for resolved tables
                import re
                name_patterns = [f"(?i)(?:^|\\.){re.escape(n)}$" for n in all_resolved_names] if all_resolved_names else None
                
                _t0 = _time.perf_counter()
                ctx = await context_hub.schema_builder.build(
                    with_stats=True,
                    data_source_ids=ds_scope,
                    name_patterns=name_patterns,
                )
                logger.info(
                    "create_data.schema_build stage=final_excerpt elapsed_ms=%.0f ds_count=%d patterns=%d",
                    (_time.perf_counter() - _t0) * 1000.0,
                    len(ds_scope or []),
                    len(name_patterns or []),
                )
                schemas_excerpt = ctx.render_combined(top_k_per_ds=20, index_limit=0, include_index=False)
            except Exception as e:
                # Fallback to keyword-based excerpt if resolution-based build fails
                raw_text = (data.interpreted_prompt or data.user_prompt or "")
                schemas_excerpt = await self._build_schemas_excerpt(context_hub, context_view, raw_text, top_k=10)
        else:
            # File-only mode: no database schemas needed
            schemas_excerpt = ""

        # Static and warm sections for prompt grounding
        _resources_section_obj = getattr(context_view.static, "resources", None) if context_view else None
        resources_context = _resources_section_obj.render() if _resources_section_obj else ""
        _files_section_obj = getattr(context_view.static, "files", None) if context_view else None
        files_context = _files_section_obj.render() if _files_section_obj else ""
        _instructions_section_obj = getattr(context_view.static, "instructions", None) if context_view else None
        instructions_context = _instructions_section_obj.render() if _instructions_section_obj else ""
        _messages_section_obj = getattr(context_view.warm, "messages", None) if context_view else None
        messages_context = _messages_section_obj.render() if _messages_section_obj else ""
        _mentions_section_obj = getattr(context_view.static, "mentions", None) if context_view else None
        mentions_context = _mentions_section_obj.render() if _mentions_section_obj else "<mentions>No mentions for this turn</mentions>"
        _entities_section_obj = getattr(context_view.warm, "entities", None) if context_view else None
        entities_context = _entities_section_obj.render() if _entities_section_obj else ""

        # Past observations and history summary
        past_observations = []
        last_observation = None
        if context_hub and getattr(context_hub, "observation_builder", None):
            try:
                past_observations = context_hub.observation_builder.tool_observations or []
                last_observation = context_hub.observation_builder.get_latest_observation()
            except Exception:
                past_observations = []
                last_observation = None
        history_summary = ""
        if context_hub and hasattr(context_hub, "get_history_summary"):
            try:
                history_summary = context_hub.get_history_summary()
            except Exception:
                history_summary = ""

        # Code generation and execution with retries
        run_span.add_event("context_built")
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "init_code_execution"})

        base_usage_ctx = runtime_ctx.get("usage_limit_context")
        usage_ctx = (
            base_usage_ctx.for_source("create_data", runtime_ctx.get("tool_call_id"))
            if isinstance(base_usage_ctx, UsageLimitContext)
            else None
        )
        coder = Coder(
            model=runtime_ctx.get("model"),
            organization_settings=organization_settings,
            context_hub=context_hub,
            usage_session_maker=async_session_maker,
            usage_context=usage_ctx,
        )
        streamer = StreamingCodeExecutor(
            organization_settings=organization_settings,
            logger=None,
            context_hub=context_hub,
            usage_context=usage_ctx,
        )

        # Build typed context via helper (use resolved active tables, not original patterns)
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "building_context"})
        codegen_context = await build_codegen_context(
            runtime_ctx=runtime_ctx,
            user_prompt=(data.user_prompt or data.interpreted_prompt or ""),
            interpreted_prompt=(data.interpreted_prompt or None),
            schemas_excerpt=(schemas_excerpt or ""),
            tables_by_source=resolved_tables or None,
            target_visualization_type=(early_viz_type if early_viz_type and early_viz_type != "table" else None),
        )

        # Combine schemas with files for additional grounding (keep previous semantics)
        schemas = (codegen_context.schemas_excerpt or "") + ("\n\n" + codegen_context.files_context if codegen_context.files_context else "")

        code_errors = []
        generated_code = None
        exec_df = None
        output_log = ""
        executed_queries = []
        query_timings = []
        codegen_ms = None
        execution_ms = None

        # Resolver for load_step()/load_entity() calls the generated code may
        # make — scoped to this report's steps and the user's accessible
        # entities.
        from app.ai.code_execution.loadables import LoadablesResolver
        _loadables_resolver = LoadablesResolver(
            db=runtime_ctx.get("db"),
            organization=runtime_ctx.get("organization"),
            report=runtime_ctx.get("report"),
            current_user=runtime_ctx.get("user"),
        )

        with tracer.start_as_current_span("create_data.codegen_and_execute") as codegen_span:
            async for e in streamer.generate_and_execute_stream_v2(
                request=CodeGenRequest(context=codegen_context, retries=2),
                ds_clients=runtime_ctx.get("ds_clients", {}),
                excel_files=runtime_ctx.get("excel_files", []),
                code_context_builder=None,
                code_generator_fn=coder.generate_code,
                sigkill_event=runtime_ctx.get("sigkill_event"),
                loadable_resolver_fn=_loadables_resolver.resolve,
            ):
                if e["type"] == "progress":
                    # Map internal stage names to UI-friendly names
                    mapped = dict(e["payload"])
                    _stage_map = {
                        "code_generation": "generating_code",
                        "code_generated": "generated_code",
                        "data_query_execution": "executing_code",
                    }
                    if mapped.get("stage") in _stage_map:
                        mapped["stage"] = _stage_map[mapped["stage"]]
                    yield ToolProgressEvent(type="tool.progress", payload=mapped)
                elif e["type"] == "stdout":
                    yield ToolStdoutEvent(type="tool.stdout", payload=e["payload"])
                elif e["type"] == "security_violation":
                    _vtype = e["payload"].get("violation_type", "unknown")
                    _action = "security.unsafe_code_blocked" if _vtype == "unsafe_python" else "security.unsafe_sql_blocked"
                    await log_tool_audit(
                        runtime_ctx,
                        action=_action,
                        resource_type="report",
                        resource_id=str(runtime_ctx.get("report").id) if runtime_ctx.get("report") else None,
                        details={
                            "tool": "create_data",
                            "violation_type": _vtype,
                            "message": e["payload"].get("message", "")[:300],
                            "code_snippet": e["payload"].get("code_snippet", "")[:300],
                        },
                    )
                elif e["type"] == "done":
                    generated_code = e["payload"].get("code")
                    code_errors = e["payload"].get("errors") or []
                    output_log = e["payload"].get("execution_log") or ""
                    exec_df = e["payload"].get("df")
                    executed_queries = e["payload"].get("executed_queries") or []
                    query_timings = e["payload"].get("query_timings") or []
                    codegen_ms = e["payload"].get("codegen_ms")
                    execution_ms = e["payload"].get("execution_ms")
            codegen_span.set_attribute("codegen.success", generated_code is not None and exec_df is not None)
            codegen_span.set_attribute("codegen.error_count", len(code_errors))
            codegen_span.set_attribute("codegen.query_count", len(executed_queries))

        if generated_code is None or exec_df is None:
            # Audit: tool execution failure
            _ds_ids = list({g.get("data_source_id") for g in resolved_tables if g.get("data_source_id")})
            _tables = [t for g in resolved_tables for t in g.get("tables", [])]
            _last_err = ""
            try:
                _last_err = str(code_errors[-1][1])[:300] if code_errors else ""
            except Exception:
                pass
            await log_tool_audit(
                runtime_ctx,
                action="tool.data_query_failed",
                resource_type="report",
                resource_id=str(runtime_ctx.get("report").id) if runtime_ctx.get("report") else None,
                details={
                    "tool": "create_data",
                    "error_type": "execution_failure",
                    "error_message": _last_err,
                    "data_source_ids": _ds_ids,
                    "tables_requested": _tables,
                    "executed_queries": _truncate_queries(executed_queries),
                },
            )

            current_step_id = runtime_ctx.get("current_step_id")
            error_observation = {
                "summary": "Create data failed",
                "error": {
                    "type": "execution_failure",
                    "message": "execution failed (validation or execution error)",
                },
            }
            summary = self._summarize_errors(code_errors)
            # Merge summary fields without clobbering error.type
            if summary.get("retry_summary"):
                error_observation["retry_summary"] = summary["retry_summary"]
            if summary.get("error_message"):
                error_observation["error"]["message"] = summary["error_message"]
            if summary.get("error_detail"):
                error_observation["error"]["detail"] = summary["error_detail"]

            # Surface the DB-level error and failing SQL — these come from the
            # QueryCapturingClientWrapper and are much more actionable for the
            # planner than the Python-level "Execution error: ..." string.
            try:
                failed_timings = [t for t in (query_timings or []) if t.get("error")]
                if failed_timings:
                    last_failed = failed_timings[-1]
                    error_observation["error"]["db_message"] = last_failed.get("error")
                    if last_failed.get("sql"):
                        error_observation["error"]["failed_sql"] = last_failed["sql"]
            except Exception:
                # Never let observation assembly mask the primary failure.
                pass

            if current_step_id:
                error_observation["step_id"] = current_step_id
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {
                        "success": False,
                        "code": generated_code or "",
                        "data": {},
                        "data_preview": {},
                        "stats": {},
                        "execution_log": output_log,
                        "errors": code_errors,
                        "executed_queries": executed_queries,
                        "query_timings": query_timings,
                    },
                    "observation": error_observation,
                },
            )
            return

        # Audit: successful data query
        _ds_ids = list({g.get("data_source_id") for g in resolved_tables if g.get("data_source_id")})
        _tables = [t for g in resolved_tables for t in g.get("tables", [])]
        await log_tool_audit(
            runtime_ctx,
            action="tool.data_queried",
            resource_type="report",
            resource_id=str(runtime_ctx.get("report").id) if runtime_ctx.get("report") else None,
            details={
                "tool": "create_data",
                "data_source_ids": _ds_ids,
                "tables_accessed": _tables,
                "executed_queries": _truncate_queries(executed_queries),
                "row_count": len(exec_df) if exec_df is not None else 0,
            },
        )

        # Success path: format data and privacy-aware preview
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "formatting_widget"})
        formatted = streamer.format_df_for_widget(exec_df)
        info = formatted.get("info", {})
        allow_llm_see_data = organization_settings.get_config("allow_llm_see_data").value if organization_settings else True
        data_preview = build_data_preview(formatted, allow_llm_see_data=allow_llm_see_data)

        # Optional: infer minimal visualization model (type + series) using the existing DataModel schema
        inferred_dm = None
        try:
            requested_type = None
            try:
                requested_type = str((tool_input or {}).get("visualization_type") or "").strip()
            except Exception:
                requested_type = None
            effective_type = requested_type if requested_type else "table"
            if effective_type != "table":
                yield ToolProgressEvent(type="tool.progress", payload={"stage": "inferring_visualization"})
                inference = await self._infer_visualization_model(
                    runtime_ctx=runtime_ctx,
                    user_prompt=(data.user_prompt or data.interpreted_prompt or ""),
                    messages_context=codegen_context.messages_context,
                    formatted=formatted,
                    allow_llm_see_data=allow_llm_see_data,
                )
                inferred_dm = (inference or {}).get("data_model")
                progress_event = (inference or {}).get("progress_event")
                if progress_event is not None:
                    # emit the series_configured progress for UI if a non-table chart was chosen
                    yield progress_event
                # Emit visualization_inferred event with details for UI
                if inferred_dm:
                    viz_payload = {
                        "stage": "visualization_inferred",
                        "chart_type": inferred_dm.get("type"),
                        "series": inferred_dm.get("series", []),
                        "group_by": inferred_dm.get("group_by"),
                        "timing": False,
                    }
                    yield ToolProgressEvent(type="tool.progress", payload=viz_payload)
        except Exception as viz_exc:
            inferred_dm = None
            progress_event = None
            # Emit visualization error event for UI
            viz_error_msg = str(viz_exc) if viz_exc else "Visualization inference failed"
            yield ToolProgressEvent(type="tool.progress", payload={
                "stage": "visualization_error",
                "error": viz_error_msg
            })

        current_step_id = runtime_ctx.get("current_step_id")
        # Always provide a minimal data_model in observation/output
        try:
            fallback_type = effective_type if 'effective_type' in locals() and effective_type else "table"
        except Exception:
            fallback_type = "table"
        # Force the final type to the early/user-requested type; only take
        # series/grouping/sort/limit/filters from inference. (filters carry the
        # default-filter that narrows a melted KPI table to the asked-for row.)
        final_dm = finalize_inferred_data_model(fallback_type, inferred_dm)
        # Deterministic guard: a single-value card must resolve to one numeric
        # cell (value column + row selector). It repairs what it can (missing
        # value column, series-name row filter via derive_kpi_row_filter) and
        # demotes the type to table when unresolvable — a card must never
        # render an arbitrary cell (the date row or a metric label).
        final_dm = ensure_single_value_card_renderable(final_dm, formatted)
        palette_theme = _infer_palette_theme(runtime_ctx) or "default"
        # Extract available column names from formatted data for fallback inference
        available_columns = [c.get("field") for c in formatted.get("columns", []) if c.get("field")]
        view_schema = build_view_from_data_model(final_dm, title=data.title, palette_theme=palette_theme, available_columns=available_columns)
        view_payload = view_schema.model_dump(exclude_none=True) if view_schema else None
        if not view_payload and final_dm.get("type"):
            view_payload = {"version": "v2", "view": {"type": final_dm.get("type")}}

        row_count = info.get("total_rows", len(formatted.get("rows", [])))
        column_names = [
            str(c.get("field") or c.get("headerName"))
            for c in formatted.get("columns", [])
            if isinstance(c, dict) and (c.get("field") or c.get("headerName"))
        ]
        summary_parts = [
            f"Created data '{data.title}' successfully",
            f"{row_count} rows x {len(column_names)} cols",
        ]
        if column_names:
            shown_cols = ", ".join(column_names[:10])
            if len(column_names) > 10:
                shown_cols += f" (+{len(column_names) - 10} more)"
            summary_parts.append(f"cols: {shown_cols}")
        try:
            dm_type = str(final_dm.get("type") or "").strip()
            if dm_type and dm_type != "table":
                summary_parts.append(f"chart: {dm_type}")
        except Exception:
            pass
        result_summary = "; ".join(summary_parts) + "."

        observation = {
            "summary": result_summary,
            "data_preview": data_preview,
            "stats": info if allow_llm_see_data else gate_stats_for_privacy(info),
            "analysis_complete": False,
            "final_answer": None,
        }
        observation["data_model"] = final_dm
        if view_payload:
            observation["view"] = view_payload
        if current_step_id:
            observation["step_id"] = current_step_id
        run_span.set_attribute("tool.success", True)
        run_span.set_attribute("tool.chart_type", final_dm.get("type", "table"))
        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {
                    "success": True,
                    "code": generated_code,
                    "data": formatted,
                    "data_preview": data_preview,
                    "stats": info,
                    "execution_log": output_log,
                    "errors": code_errors,
                    "data_model": final_dm,
                    "view": view_payload,
                    "executed_queries": executed_queries,
                    "query_timings": query_timings,
                    "codegen_ms": codegen_ms,
                    "execution_ms": execution_ms,
                },
                "observation": observation,
            },
        )
