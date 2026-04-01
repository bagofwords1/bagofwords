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


def _build_cartesian(data_model: dict, viz_index: int) -> str:
    """Bar / line / area chart option code."""
    dm_type = (data_model.get("type") or "bar_chart").lower()
    series_list = data_model.get("series") or []
    if not series_list:
        return "{}"

    category_key = (series_list[0].get("key") or "").lower()
    if not category_key:
        return "{}"

    group_by = (data_model.get("group_by") or "").lower()
    is_horizontal = data_model.get("horizontal", False)
    chart_type = "line" if dm_type in ("line_chart", "area_chart") else "bar"
    is_area = dm_type == "area_chart"
    smooth = "true" if dm_type in ("line_chart", "area_chart") else "false"

    rows_ref = f"viz[{viz_index}].rows"

    if group_by:
        # Multi-series via group_by — use an IIFE
        value_key = (series_list[0].get("value") or "").lower()
        if not value_key:
            return "{}"

        area_block = ""
        if is_area:
            area_block = "areaStyle: {}, "

        code = (
            f"(() => {{\n"
            f"  const rows = {rows_ref};\n"
            f"  const cats = [...new Set(rows.map(r => r['{_js_str(category_key)}']))];\n"
            f"  const groups = [...new Set(rows.map(r => r['{_js_str(group_by)}']))].filter(Boolean);\n"
            f"  return {{\n"
            f"    tooltip: {{ trigger: 'axis' }},\n"
            f"    legend: {{ show: groups.length > 1, top: 0 }},\n"
        )
        if is_horizontal:
            code += (
                f"    yAxis: {{ type: 'category', data: cats }},\n"
                f"    xAxis: {{ type: 'value' }},\n"
            )
        else:
            code += (
                f"    xAxis: {{ type: 'category', data: cats }},\n"
                f"    yAxis: {{ type: 'value' }},\n"
            )
        code += (
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
        value_key = (s.get("value") or "").lower()
        if not value_key:
            continue
        name = _js_str(s.get("name") or value_key)
        area_str = "areaStyle: {}, " if is_area else ""
        series_js_parts.append(
            f"{{ name: '{name}', type: '{chart_type}', smooth: {smooth}, {area_str}"
            f"data: {rows_ref}.map(r => Number(r['{_js_str(value_key)}'])) }}"
        )

    if not series_js_parts:
        return "{}"

    series_js = ", ".join(series_js_parts)

    if is_horizontal:
        axes = (
            f"yAxis: {{ type: 'category', data: {rows_ref}.map(r => r['{_js_str(category_key)}']) }},\n"
            f"    xAxis: {{ type: 'value' }}"
        )
    else:
        axes = (
            f"xAxis: {{ type: 'category', data: {rows_ref}.map(r => r['{_js_str(category_key)}']) }},\n"
            f"    yAxis: {{ type: 'value' }}"
        )

    return (
        f"{{ tooltip: {{ trigger: 'axis' }},\n"
        f"    {axes},\n"
        f"    series: [{series_js}] }}"
    )


def _build_pie(data_model: dict, viz_index: int) -> str:
    """Pie chart option code."""
    series_list = data_model.get("series") or []
    if not series_list:
        return "{}"

    cfg = series_list[0]
    key = (cfg.get("key") or "").lower()
    value = (cfg.get("value") or "").lower()
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
    x_key = (cfg.get("x") or cfg.get("key") or "").lower()
    y_key = (cfg.get("y") or cfg.get("value") or "").lower()
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
    x_key = (cfg.get("x") or cfg.get("key") or "").lower()
    y_key = (cfg.get("y") or "").lower()
    v_key = (cfg.get("value") or "").lower()
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


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_BUILDERS = {
    "bar_chart": _build_cartesian,
    "line_chart": _build_cartesian,
    "area_chart": _build_cartesian,
    "pie_chart": _build_pie,
    "scatter_plot": _build_scatter,
    "heatmap": _build_heatmap,
}


def generate_echart_option_code(data_model: dict, viz_index: int) -> str:
    """Map a visualization's data_model to an ECharts option JS expression.

    Returns a JS expression string referencing ``viz[N].rows``.
    """
    dm_type = (data_model.get("type") or "bar_chart").lower()
    builder = _BUILDERS.get(dm_type, _build_cartesian)
    return builder(data_model, viz_index)


# ---------------------------------------------------------------------------
# JSX wrappers
# ---------------------------------------------------------------------------

def generate_section_jsx(title: str, option_code: str, height: int = 350) -> str:
    """Wrap an ECharts option expression in a <SectionCard><EChart /> block."""
    safe_title = _js_str(title)
    return (
        f'      <SectionCard title="{safe_title}">\n'
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


def inject_section_into_code(existing_code: str, new_section: str) -> Optional[str]:
    """Insert a new <SectionCard> block into existing artifact code.

    Strategy: find ``ReactDOM.createRoot`` and walk backwards to the last
    ``</div>`` before it — that's the outermost container's closing tag.
    Insert the new section right before it.

    Returns the modified code, or None if the injection point can't be found.
    """
    # Find the ReactDOM.createRoot anchor
    anchor_match = re.search(r"ReactDOM\.createRoot", existing_code)
    if not anchor_match:
        return None

    anchor_pos = anchor_match.start()

    # Search backwards from anchor for the last </div>
    code_before_anchor = existing_code[:anchor_pos]
    last_div_close = code_before_anchor.rfind("</div>")
    if last_div_close == -1:
        # Try fragment closing tag </>
        last_div_close = code_before_anchor.rfind("</>")
        if last_div_close == -1:
            return None

    # Find the indentation of the closing tag for consistent formatting
    line_start = code_before_anchor.rfind("\n", 0, last_div_close)
    if line_start == -1:
        line_start = 0
    else:
        line_start += 1  # skip the newline itself

    # Insert the new section before the closing tag
    return (
        existing_code[:last_div_close]
        + new_section + "\n"
        + existing_code[last_div_close:]
    )
