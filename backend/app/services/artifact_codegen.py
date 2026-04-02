"""Programmatic artifact code generation from data_model → ECharts JS.

Pure string generation — no LLM, no DB, no async.
Translates a visualization's data_model into the same React/ECharts JSX
that the LLM would produce, but instantly.
"""

import re
from typing import List, Optional


# ---------------------------------------------------------------------------
# Chart-type → ECharts option JS builders
# ---------------------------------------------------------------------------

def _js_str(s: str) -> str:
    """Escape a string for safe embedding in JS single-quoted literals."""
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")


# Runtime JS helper: compute axis-label rotation from category count.
# Mirrors the heuristic in EChartsVisual.vue → getAxisLabelConfig().
_AXIS_LABEL_HEURISTIC = (
    "const _rot = cats.length > 5 ? 45 : 0;\n"
    "  const _interval = cats.length > 50 "
    "? Math.max(1, Math.floor(cats.length / 20)) "
    ": (cats.length > 25 ? 1 : 0);\n"
)


def _build_cartesian(data_model: dict, viz_index: int) -> str:
    """Bar / line / area chart option code.

    Always emits an IIFE so we can compute categories + rotation at runtime.
    """
    dm_type = (data_model.get("type") or "bar_chart").lower()
    series_list = data_model.get("series") or []
    if not series_list:
        return "{}"

    category_key = series_list[0].get("key") or ""
    if not category_key:
        return "{}"

    group_by = data_model.get("group_by") or ""
    is_horizontal = data_model.get("horizontal", False)
    chart_type = "line" if dm_type in ("line_chart", "area_chart") else "bar"
    is_area = dm_type == "area_chart"
    smooth = "true" if dm_type in ("line_chart", "area_chart") else "false"

    rows_ref = f"viz[{viz_index}].rows"

    # --- preamble (shared for group_by and traditional) ---
    code = (
        f"(() => {{\n"
        f"  const rows = {rows_ref};\n"
        f"  const cats = [...new Set(rows.map(r => r['{_js_str(category_key)}']))];\n"
        f"  {_AXIS_LABEL_HEURISTIC}"
    )

    # category axis with rotation
    if is_horizontal:
        cat_axis = "yAxis: { type: 'category', data: cats, axisLabel: { rotate: 0 } }"
        val_axis = "xAxis: { type: 'value' }"
    else:
        cat_axis = "xAxis: { type: 'category', data: cats, axisLabel: { rotate: _rot, interval: _interval, hideOverlap: true } }"
        val_axis = "yAxis: { type: 'value' }"

    if group_by:
        value_key = series_list[0].get("value") or ""
        if not value_key:
            return "{}"

        area_block = "areaStyle: {}, " if is_area else ""

        code += (
            f"  const groups = [...new Set(rows.map(r => r['{_js_str(group_by)}']))].filter(Boolean);\n"
            f"  return {{\n"
            f"    tooltip: {{ trigger: 'axis' }},\n"
            f"    legend: {{ show: groups.length > 1, top: 0 }},\n"
            f"    {cat_axis},\n"
            f"    {val_axis},\n"
            f"    series: groups.map(g => ({{\n"
            f"      name: g, type: '{chart_type}', smooth: {smooth}, {area_block}\n"
            f"      data: cats.map(c => {{\n"
            f"        const row = rows.find(r => r['{_js_str(category_key)}'] === c && r['{_js_str(group_by)}'] === g);\n"
            f"        return row ? Number(row['{_js_str(value_key)}']) : null;\n"
            f"      }})\n"
            f"    }}))\n"
            f"  }};\n"
            f"}})()"
        )
        return code

    # Traditional: one series per series config entry
    series_js_parts: list[str] = []
    for s in series_list:
        value_key = s.get("value") or ""
        if not value_key:
            continue
        name = _js_str(s.get("name") or value_key)
        area_str = "areaStyle: {}, " if is_area else ""
        series_js_parts.append(
            f"{{ name: '{name}', type: '{chart_type}', smooth: {smooth}, {area_str}"
            f"data: cats.map(c => {{ const row = rows.find(r => r['{_js_str(category_key)}'] === c); "
            f"return row ? Number(row['{_js_str(value_key)}']) : null; }}) }}"
        )

    if not series_js_parts:
        return "{}"

    series_js = ", ".join(series_js_parts)

    code += (
        f"  return {{\n"
        f"    tooltip: {{ trigger: 'axis' }},\n"
        f"    {cat_axis},\n"
        f"    {val_axis},\n"
        f"    series: [{series_js}]\n"
        f"  }};\n"
        f"}})()"
    )
    return code


def _build_pie(data_model: dict, viz_index: int) -> str:
    """Pie chart option code."""
    series_list = data_model.get("series") or []
    if not series_list:
        return "{}"

    cfg = series_list[0]
    key = cfg.get("key") or ""
    value = cfg.get("value") or ""
    if not key or not value:
        return "{}"

    rows_ref = f"viz[{viz_index}].rows"
    return (
        f"{{ tooltip: {{ trigger: 'item', formatter: '{{b}}: {{c}} ({{d}}%)' }},\n"
        f"    series: [{{ type: 'pie', radius: ['40%', '70%'],\n"
        f"      data: {rows_ref}.map(r => ({{ name: r['{_js_str(key)}'], value: Number(r['{_js_str(value)}']) }}))\n"
        f"        .filter(d => d.name != null && !isNaN(d.value)) }}] }}"
    )


def _build_scatter(data_model: dict, viz_index: int) -> str:
    """Scatter plot option code."""
    series_list = data_model.get("series") or []
    if not series_list:
        return "{}"

    cfg = series_list[0]
    x_key = cfg.get("x") or cfg.get("key") or ""
    y_key = cfg.get("y") or cfg.get("value") or ""
    if not x_key or not y_key:
        return "{}"

    rows_ref = f"viz[{viz_index}].rows"
    return (
        f"{{ tooltip: {{ trigger: 'item' }},\n"
        f"    xAxis: {{ type: 'value', name: '{_js_str(x_key)}' }},\n"
        f"    yAxis: {{ type: 'value', name: '{_js_str(y_key)}' }},\n"
        f"    series: [{{ type: 'scatter',\n"
        f"      data: {rows_ref}.map(r => [Number(r['{_js_str(x_key)}']), Number(r['{_js_str(y_key)}'])])\n"
        f"        .filter(d => !d.some(v => isNaN(v))) }}] }}"
    )


def _build_heatmap(data_model: dict, viz_index: int) -> str:
    """Heatmap option code."""
    series_list = data_model.get("series") or []
    if not series_list:
        return "{}"

    cfg = series_list[0]
    x_key = cfg.get("x") or cfg.get("key") or ""
    y_key = cfg.get("y") or ""
    v_key = cfg.get("value") or ""
    if not x_key or not y_key or not v_key:
        return "{}"

    rows_ref = f"viz[{viz_index}].rows"
    return (
        f"(() => {{\n"
        f"  const rows = {rows_ref};\n"
        f"  const xCats = [...new Set(rows.map(r => String(r['{_js_str(x_key)}'] ?? '')))];\n"
        f"  const yCats = [...new Set(rows.map(r => String(r['{_js_str(y_key)}'] ?? '')))];\n"
        f"  const data = rows.map(r => {{\n"
        f"    const xi = xCats.indexOf(String(r['{_js_str(x_key)}'] ?? ''));\n"
        f"    const yi = yCats.indexOf(String(r['{_js_str(y_key)}'] ?? ''));\n"
        f"    const v = Number(r['{_js_str(v_key)}']);\n"
        f"    return (xi >= 0 && yi >= 0 && !isNaN(v)) ? [xi, yi, v] : null;\n"
        f"  }}).filter(Boolean);\n"
        f"  const vals = data.map(d => d[2]);\n"
        f"  return {{\n"
        f"    tooltip: {{ position: 'top' }},\n"
        f"    xAxis: {{ type: 'category', data: xCats }},\n"
        f"    yAxis: {{ type: 'category', data: yCats }},\n"
        f"    visualMap: {{ min: Math.min(...vals), max: Math.max(...vals), orient: 'horizontal', left: 'center', bottom: '5%', calculable: true }},\n"
        f"    series: [{{ type: 'heatmap', data, label: {{ show: true, formatter: '{{@[2]}}' }} }}]\n"
        f"  }};\n"
        f"}})()"
    )


def _build_metric_card(data_model: dict, viz_index: int) -> str:
    """Metric card / KPI — renders viz[N].rows[0][valueField] via KPICard."""
    series_list = data_model.get("series") or []
    value_key = ""
    if series_list:
        value_key = series_list[0].get("value") or ""

    v = f"viz[{viz_index}]"

    if value_key:
        safe_key = _js_str(value_key)
        value_expr = f"{v}.rows[0]['{safe_key}']"
    else:
        # Fallback: first numeric value in the row
        value_expr = (
            f"(() => {{ const r = {v}.rows[0]; "
            f"const k = Object.keys(r).find(k => typeof r[k] === 'number'); "
            f"return k ? r[k] : Object.values(r)[0]; }})()"
        )

    name = _js_str(series_list[0].get("name") or "") if series_list else ""
    # Format: add commas for large numbers, keep 1 decimal for floats
    return (
        f"(() => {{\n"
        f"  const row = ({v}.rows || [])[0];\n"
        f"  if (!row) return null;\n"
        f"  const raw = {value_expr};\n"
        f"  const val = typeof raw === 'number'\n"
        f"    ? (raw % 1 === 0 ? raw.toLocaleString() : raw.toLocaleString(undefined, {{minimumFractionDigits: 1, maximumFractionDigits: 2}}))\n"
        f"    : String(raw ?? '');\n"
        f"  return <KPICard title=\"{name}\" value={{val}} />;\n"
        f"}})()"
    )


def _build_table(data_model: dict, viz_index: int) -> str:
    """Table JSX — renders viz[N].columns / viz[N].rows as a Tailwind table."""
    v = f"viz[{viz_index}]"
    return (
        f"(() => {{\n"
        f"  const cols = {v}.columns || [];\n"
        f"  const rows = {v}.rows || [];\n"
        f"  return <div style={{{{maxHeight: 400, overflow: 'auto'}}}}>\n"
        f'    <table className="w-full text-sm text-left">\n'
        f'      <thead className="text-xs uppercase bg-slate-50 sticky top-0">\n'
        f"        <tr>{{cols.map((c, i) =>\n"
        f'          <th key={{i}} className="px-4 py-3 font-medium text-slate-500">{{c.headerName || c.field}}</th>\n'
        f"        )}}</tr>\n"
        f"      </thead>\n"
        f"      <tbody>\n"
        f"        {{rows.map((row, i) =>\n"
        f'          <tr key={{i}} className="border-b border-slate-100 hover:bg-slate-50">\n'
        f"            {{cols.map((c, j) =>\n"
        f'              <td key={{j}} className="px-4 py-2 text-slate-700">{{row[c.field] != null ? String(row[c.field]) : \'\'}}</td>\n'
        f"            )}}\n"
        f"          </tr>\n"
        f"        )}}\n"
        f"      </tbody>\n"
        f"    </table>\n"
        f"  </div>;\n"
        f"}})()"
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

# Builders that return an ECharts option JS expression
_CHART_BUILDERS = {
    "bar_chart": _build_cartesian,
    "line_chart": _build_cartesian,
    "area_chart": _build_cartesian,
    "pie_chart": _build_pie,
    "scatter_plot": _build_scatter,
    "heatmap": _build_heatmap,
}

# Types that produce raw JSX (not an EChart option)
_JSX_BUILDERS = {
    "table": _build_table,
    "metric_card": _build_metric_card,
}


def is_jsx_type(data_model: dict) -> bool:
    """Check if the data_model type produces raw JSX (not an EChart)."""
    dm_type = (data_model.get("type") or "").lower()
    return dm_type in _JSX_BUILDERS


# Keep backward-compatible alias
is_table_type = is_jsx_type


def generate_echart_option_code(data_model: dict, viz_index: int) -> str:
    """Map a visualization's data_model to an ECharts option JS expression.

    Returns a JS expression string referencing ``viz[N].rows``.
    For table types, use ``generate_table_jsx`` instead.
    """
    dm_type = (data_model.get("type") or "bar_chart").lower()
    builder = _CHART_BUILDERS.get(dm_type, _build_cartesian)
    return builder(data_model, viz_index)


def generate_jsx_section(title: str, data_model: dict, viz_index: int) -> str:
    """Generate JSX for a non-chart visualization (table, metric_card, etc.)."""
    dm_type = (data_model.get("type") or "table").lower()
    builder = _JSX_BUILDERS.get(dm_type, _build_table)
    jsx_expr = builder(data_model, viz_index)

    # KPICard is self-contained — no SectionCard wrapper needed
    if dm_type == "metric_card":
        return f"{{{jsx_expr}}}"

    safe_title = _js_str(title)
    return (
        f'<SectionCard title="{safe_title}">\n'
        f"        {{{jsx_expr}}}\n"
        f"      </SectionCard>"
    )


# Keep backward-compatible alias
generate_table_jsx = generate_jsx_section


# ---------------------------------------------------------------------------
# JSX wrappers
# ---------------------------------------------------------------------------

def generate_section_jsx(title: str, option_code: str, height: int = 350) -> str:
    """Wrap an ECharts option expression in a <SectionCard><EChart /> block."""
    safe_title = _js_str(title)
    return (
        f'<SectionCard title="{safe_title}">\n'
        f"        <EChart height={{{height}}} option={{{option_code}}} />\n"
        f"      </SectionCard>"
    )


def generate_scaffold(sections: List[str]) -> str:
    """Generate a complete artifact <script> block from a list of section JSX strings."""
    joined = "\n".join(sections)
    return (
        '<script type="text/babel">\n'
        "function App() {\n"
        "  const data = useArtifactData();\n"
        '  if (!data) return <div className="flex items-center justify-center h-screen"><LoadingSpinner /></div>;\n'
        "  const viz = data.visualizations;\n"
        "  return (\n"
        '    <div className="min-h-full bg-gradient-to-br from-slate-50 to-slate-100 p-8 space-y-6">\n'
        f"{joined}\n"
        "    </div>\n"
        "  );\n"
        "}\n"
        "ReactDOM.createRoot(document.getElementById('root')).render(<App />);\n"
        "</script>"
    )


def inject_section_into_code(
    existing_code: str,
    title: str,
    data_model: dict,
    viz_index: int,
) -> Optional[str]:
    """Append a new visualization section to existing artifact code.

    Strategy: find the closing ``</script>`` tag and insert a self-contained
    IIFE just before it.  The IIFE creates its own container element and
    React root, so it never touches the existing LLM-generated JSX tree.

    The option code is computed in a ``var`` statement *before* the JSX return
    to avoid nested-brace parsing issues in Babel.

    Returns the modified code, or None if ``</script>`` can't be found.
    """
    script_close_pos = existing_code.rfind("</script>")
    if script_close_pos == -1:
        return None

    safe_title = _js_str(title)
    dm_type = (data_model.get("type") or "").lower()

    if dm_type in _JSX_BUILDERS:
        jsx_section = generate_jsx_section(title, data_model, viz_index)
        body = (
            f"    return <div className=\"space-y-6\">\n"
            f"      {jsx_section}\n"
            f"    </div>;\n"
        )
    else:
        option_code = generate_echart_option_code(data_model, viz_index)
        body = (
            f"    var _opt = {option_code};\n"
            f"    return <div className=\"space-y-6\">\n"
            f'      <SectionCard title="{safe_title}">\n'
            f"        <EChart height={{350}} option={{_opt}} />\n"
            f"      </SectionCard>\n"
            f"    </div>;\n"
        )

    addition = (
        "\n\n// --- Programmatically added visualization ---\n"
        "(function() {\n"
        "  function AddedViz() {\n"
        "    var data = useArtifactData();\n"
        "    if (!data) return null;\n"
        "    var viz = data.visualizations;\n"
        f"{body}"
        "  }\n"
        "  var _obs = new MutationObserver(function() {\n"
        "    var root = document.getElementById('root');\n"
        "    var container = root && root.firstElementChild;\n"
        "    if (container) {\n"
        "      _obs.disconnect();\n"
        "      var _el = document.createElement('div');\n"
        "      container.appendChild(_el);\n"
        "      ReactDOM.createRoot(_el).render(React.createElement(AddedViz));\n"
        "    }\n"
        "  });\n"
        "  _obs.observe(document.getElementById('root'), { childList: true, subtree: true });\n"
        "})();\n"
    )

    return existing_code[:script_close_pos] + addition + existing_code[script_close_pos:]
