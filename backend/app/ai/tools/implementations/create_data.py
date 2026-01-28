import json
import asyncio
from typing import AsyncIterator, Dict, Any, Type, Optional, List, Union
from pydantic import BaseModel

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
from app.ai.llm import LLM
from app.dependencies import async_session_maker
from app.ai.tools.schemas import DataModel
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


def _infer_palette_theme(runtime_ctx: Dict[str, Any]) -> Optional[str]:
    report_theme = runtime_ctx.get("report_theme_name")
    if report_theme:
        return str(report_theme)
    org_settings = runtime_ctx.get("settings")
    try:
        return str(org_settings.get_config("default_theme").value)
    except Exception:
        return None


def _build_series_styles(series: List[Dict[str, Any]]) -> List[SeriesStyle]:
    styles: List[SeriesStyle] = []
    for entry in series or []:
        key = entry.get("value") or entry.get("name")
        if not key:
            continue
        label = entry.get("name")
        try:
            styles.append(SeriesStyle(key=str(key), label=label))
        except Exception:
            continue
    return styles


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
        # Show legend if multiple series or groupBy is used
        has_multiple_series = len(series) > 1 or data_model.get("group_by")
        view_cls = {
            "bar_chart": BarChartView,
            "line_chart": LineChartView,
            "area_chart": AreaChartView,
        }.get(chart_type, BarChartView)
        view = view_cls(
            title=title,
            x=str(x_key),
            y=y_value,
            groupBy=data_model.get("group_by"),
            palette=palette,
            seriesStyles=series_styles,
            legend=LegendOptions(show=bool(has_multiple_series)),
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
        )
        return ViewSchema(view=view)

    if chart_type == "table":
        view = TableView(title=title)
        return ViewSchema(view=view)

    # CountView - simple single value display (value is optional)
    if chart_type == "count":
        base = series[0] if series else {}
        value_key = base.get("value") or base.get("metric") or base.get("key") or base.get("name")
        view = CountView(
            title=title,
            value=str(value_key) if value_key else None,
            palette=palette,
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
            view = CountView(title=title, palette=palette)
            return ViewSchema(view=view)
        
        view = MetricCardView(
            title=title,
            value=str(value_key),
            comparison=str(comparison_key) if comparison_key else None,
            comparisonLabel=comparison_label,
            invertTrend=invert_trend,
            sparkline=sparkline,
            palette=palette,
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
            cols.append({
                "name": name,
                "dtype": meta.get("dtype"),
                "non_null_count": meta.get("non_null_count"),
                "unique_count": meta.get("unique_count"),
                "null_count": meta.get("null_count"),
                "min": meta.get("min"),
                "max": meta.get("max"),
            })
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
        llm = LLM(runtime_ctx.get("model"), usage_session_maker=async_session_maker)
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
        
        prompt = f"""You are a visualization planner. Analyze the data profile and choose the best visualization type.
{instructions_block}
CRITICAL: You MUST use EXACT column names from the data. Available columns are: {column_names}

Context: {messages_context or "None"}
User prompt: {user_prompt or "None"}

Data profile:
{json.dumps(profile, ensure_ascii=False, indent=2)}

═══════════════════════════════════════════════════════════════════════════════
RULES FOR METRIC_CARD (KPI display)
═══════════════════════════════════════════════════════════════════════════════

Use metric_card when showing a single key metric. The "value" field MUST be an EXACT column name.

DETECTING THE VALUE COLUMN:
- Look for columns with names like: revenue, total, amount, count, sum, value, sales, profit, cost
- AVOID using date/time columns (year, month, date, week, day) as the value
- AVOID using ID columns as the value
- Pick the column that represents the METRIC the user asked about

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
- bar/line/area: [{{"name", "key", "value"}}] - BOTH key AND value are REQUIRED!
- pie/map: [{{"name", "key", "value"}}]
- scatter: [{{"name", "x", "y"}}] (+ size optional)
- heatmap: [{{"name", "x", "y", "value", "colorScheme", "showValues"}}]
  - colorScheme: "blue" | "green" | "red" | "violet" | "orange" (default: "blue")
  - showValues: true | false (default: true) - whether to show values in cells
- table: series: []

CRITICAL FOR BAR/LINE/AREA CHARTS:
- "key" = the CATEGORY column (x-axis) - REQUIRED, usually a date, name, or category column
- "value" = the NUMERIC column (y-axis) - REQUIRED, the metric to display
- You MUST include BOTH "key" and "value" in every series entry!

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
OUTPUT FORMAT
═══════════════════════════════════════════════════════════════════════════════

Return ONLY valid JSON:
{{"type": "...", "series": [...], "group_by": "column_name_or_null"}}

Include "group_by" when the data has multiple rows per x-axis category that should be shown as separate colored series.

REMEMBER: Use EXACT column names from: {column_names}
Do NOT use generic placeholders like "value" unless that's the actual column name."""

        try:
            raw = llm.inference(prompt, usage_scope="create_data.viz_infer")
        except Exception:
            raw = None

        candidate = {"type": "table", "series": []}
        view_options: Dict[str, Any] | None = None
        if raw:
            try:
                candidate_json = json.loads(raw)
            except Exception:
                candidate_json = None
            if isinstance(candidate_json, dict):
                try:
                    dm = DataModel(**{k: v for k, v in candidate_json.items() if k in {"type", "series", "group_by", "sort", "limit"}})
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

        # Emit a progress event for UI when series/type are inferred
        try:
            chart_type = candidate.get("type")
            if chart_type and chart_type != "table":
                await asyncio.sleep(0)  # keep cooperative
                payload = {
                    "stage": "series_configured",
                    "series": candidate.get("series") or [],
                    "chart_type": chart_type,
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

                ctx = await context_hub.schema_builder.build(
                    with_stats=True,
                    name_patterns=name_patterns,
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
        
        if not tables_by_source or not schema_builder:
            return [], ["No tables_by_source or schema_builder provided"]
        
        resolved: List[Dict[str, Any]] = []
        warnings: List[str] = []
        
        # Detect special regex characters
        special_chars = re.compile(r"[\^\$\.\*\+\?\[\]\(\)\{\}\|]")
        
        for group in tables_by_source:
            ds_id = str(group.data_source_id) if getattr(group, "data_source_id", None) else None
            input_tables = getattr(group, "tables", []) or []
            
            if not input_tables:
                continue
            
            # Build name_patterns from table names/patterns
            name_patterns: List[str] = []
            for name in input_tables:
                if not isinstance(name, str) or not name.strip():
                    continue
                name = name.strip()
                
                if special_chars.search(name):
                    # Already contains regex chars - use as-is (make case-insensitive)
                    name_patterns.append(f"(?i){name}")
                else:
                    # Literal name - escape and allow optional schema prefix
                    esc = re.escape(name)
                    name_patterns.append(f"(?i)(?:^|\\.){esc}$")
            
            if not name_patterns:
                continue
            
            # Resolve via schema_builder (only returns active tables)
            try:
                ctx = await schema_builder.build(
                    with_stats=False,
                    data_source_ids=[ds_id] if ds_id else None,
                    name_patterns=name_patterns,
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
        
        return resolved, warnings

    @staticmethod
    def _summarize_errors(errors) -> dict:
        last_text = (errors[-1][1] if errors else "") or ""
        last_line = last_text.strip().splitlines()[0][:300]
        payload = {
            "retry_summary": {
                "attempts": int(len(errors or [])),
                "succeeded": False,
                "error_count": int(len(errors or [])),
                "last_error_message": last_line,
            }
        }
        if last_line:
            payload["error"] = {"message": last_line}
        return payload

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_data",
            description="Generate code from prompt and execute to return data resultas table or chart. Use this when you want to generate a tracked insight, or you have enough information to generate a widget. Call create_data for 1 insight at a time. If you need to generate multiple insights, call create_data multiple times.",
            category="action",
            version="1.0.0",
            input_schema=CreateDataInput.model_json_schema(),
            output_schema=CreateDataOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=180,
            idempotent=False,
            required_permissions=[],
            tags=["data", "code", "execution"],
            allowed_modes=["chat", "deep"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreateDataInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreateDataOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = CreateDataInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": data.title})
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "init"})

        # Context and views
        organization_settings = runtime_ctx.get("settings")
        context_view = runtime_ctx.get("context_view")
        context_hub = runtime_ctx.get("context_hub")

        # Early: signal intended artifact type and request step creation before code-gen
        try:
            # Single signal: declare type and pass the intended query title
            allowed_types = ALLOWED_VIZ_TYPES
            requested_type = None
            try:
                requested_type = str((tool_input or {}).get("visualization_type") or "").strip()
            except Exception:
                requested_type = None
            viz_type = requested_type if requested_type in allowed_types else "table"
            yield ToolProgressEvent(
                type="tool.progress",
                payload={
                    "stage": "data_model_type_determined",
                    "data_model_type": viz_type,
                    "query_title": data.title,
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
        
        if total_resolved == 0 and not has_files:
            # No tables resolved AND no files available - fail
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
                
                ctx = await context_hub.schema_builder.build(
                    with_stats=True,
                    data_source_ids=ds_scope,
                    name_patterns=name_patterns,
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
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "generating_code"})

        coder = Coder(
            model=runtime_ctx.get("model"),
            organization_settings=organization_settings,
            context_hub=context_hub,
            usage_session_maker=async_session_maker,
        )
        streamer = StreamingCodeExecutor(organization_settings=organization_settings, logger=None, context_hub=context_hub)

        # Build typed context via helper (use resolved active tables, not original patterns)
        codegen_context = await build_codegen_context(
            runtime_ctx=runtime_ctx,
            user_prompt=(data.user_prompt or data.interpreted_prompt or ""),
            interpreted_prompt=(data.interpreted_prompt or None),
            schemas_excerpt=(schemas_excerpt or ""),
            tables_by_source=resolved_tables or None,
        )

        # Combine schemas with files for additional grounding (keep previous semantics)
        schemas = (codegen_context.schemas_excerpt or "") + ("\n\n" + codegen_context.files_context if codegen_context.files_context else "")

        code_errors = []
        generated_code = None
        exec_df = None
        output_log = ""
        executed_queries = []

        async for e in streamer.generate_and_execute_stream_v2(
            request=CodeGenRequest(context=codegen_context, retries=2),
            ds_clients=runtime_ctx.get("ds_clients", {}),
            excel_files=runtime_ctx.get("excel_files", []),
            code_context_builder=None,
            code_generator_fn=coder.generate_code,
            sigkill_event=runtime_ctx.get("sigkill_event"),
        ):
            if e["type"] == "progress":
                yield ToolProgressEvent(type="tool.progress", payload=e["payload"]) 
            elif e["type"] == "stdout":
                yield ToolStdoutEvent(type="tool.stdout", payload=e["payload"]) 
            elif e["type"] == "done":
                generated_code = e["payload"].get("code")
                code_errors = e["payload"].get("errors") or []
                output_log = e["payload"].get("execution_log") or ""
                exec_df = e["payload"].get("df")
                executed_queries = e["payload"].get("executed_queries") or []

        if generated_code is None or exec_df is None:
            current_step_id = runtime_ctx.get("current_step_id")
            error_observation = {
                "summary": "Create data failed",
                "error": {"type": "execution_failure", "message": "execution failed (validation or execution error)"},
            }
            try:
                error_observation.update(self._summarize_errors(code_errors))
            except Exception:
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
                    },
                    "observation": error_observation,
                },
            )
            return

        # Success path: format data and privacy-aware preview
        formatted = streamer.format_df_for_widget(exec_df)
        info = formatted.get("info", {})
        allow_llm_see_data = organization_settings.get_config("allow_llm_see_data").value if organization_settings else True
        if allow_llm_see_data:
            data_preview = {
                "columns": formatted.get("columns", []),
                "rows": formatted.get("rows", [])[:5],
            }
        else:
            data_preview = {
                "columns": [{"field": c.get("field")} for c in formatted.get("columns", [])],
                "row_count": len(formatted.get("rows", [])),
                "stats": info,
            }

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
        # Force the final type to the early/user-requested type; only take series/grouping from inference
        final_dm = {"type": fallback_type, "series": []}
        if isinstance(inferred_dm, dict):
            for key in ("series", "group_by", "sort", "limit"):
                if inferred_dm.get(key) is not None:
                    final_dm[key] = inferred_dm.get(key)
        palette_theme = _infer_palette_theme(runtime_ctx) or "default"
        # Extract available column names from formatted data for fallback inference
        available_columns = [c.get("field") for c in formatted.get("columns", []) if c.get("field")]
        view_schema = build_view_from_data_model(final_dm, title=data.title, palette_theme=palette_theme, available_columns=available_columns)
        view_payload = view_schema.model_dump(exclude_none=True) if view_schema else None
        if not view_payload and final_dm.get("type"):
            view_payload = {"version": "v2", "view": {"type": final_dm.get("type")}}

        observation = {
            "summary": f"Created data '{data.title}' successfully.",
            "data_preview": data_preview,
            "stats": info,
            "analysis_complete": False,
            "final_answer": None,
        }
        observation["data_model"] = final_dm
        if view_payload:
            observation["view"] = view_payload
        if current_step_id:
            observation["step_id"] = current_step_id
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
                },
                "observation": observation,
            },
        )


