"""
Shared sandbox runtime context for artifact tools.

Single source of truth describing the iframe sandbox environment where
LLM-generated artifact code executes. Used by create_artifact, edit_artifact,
and read_artifact to ensure the AI never misdiagnoses missing globals or
misinterprets minified React errors.
"""

# ---------------------------------------------------------------------------
# Prompt section: embedded in LLM prompts for create_artifact & edit_artifact
# ---------------------------------------------------------------------------

SANDBOX_RUNTIME_PROMPT = """
═══════════════════════════════════════════════════════════════════════════════
SANDBOX RUNTIME ENVIRONMENT (pre-loaded globally — do NOT import or redefine)
═══════════════════════════════════════════════════════════════════════════════

The generated code runs inside a sandboxed iframe. The following libraries and
helpers are **already loaded globally** — do NOT import, redefine, or remove
references to any of them:

• **React 18** — `React`, `ReactDOM` available globally
  - Hooks are also global: `useState`, `useEffect`, `useRef`, `useMemo`, `useCallback` — use directly without `React.` prefix

• **ECharts 5** — `echarts` available globally
  - Prefer the `<EChart>` wrapper component (see below) for standard charts
  - Full ECharts option API supported: bar, line, pie, scatter, radar, treemap, sunburst, heatmap, gauge, funnel, sankey, parallel, calendar, graph, etc.

• **`<EChart>`** — Global React wrapper for ECharts (handles init/dispose/resize)
  - Props: `option` (ECharts option object), `height` (number, default 400), `className` (string)
  - Usage: `<EChart height={400} option={{ xAxis: {...}, series: [...] }} />`
  - Supports ALL ECharts chart types — pass any valid ECharts option object
  - Auto-resizes via ResizeObserver
  - Uses 'bow' theme: colors, tooltip, grid, axis styling, rounded corners all pre-configured
  - For standard charts, only specify data mapping — the theme handles styling
  - For advanced charts (gauge, radar, treemap, sankey, etc.), specify the full option — the theme still provides colors and tooltip

• **Tailwind CSS (v3.4)** — All utility classes available
  - Use modern design: rounded-xl, shadow-lg, backdrop-blur, gradients
  - Dark/light themes, responsive grids, flexbox

• **Babel** — JSX is transpiled automatically
  - Code must be wrapped in `<script type="text/babel">...</script>`

• **useArtifactData()** — Global React hook
  - Returns `null` while data is loading, then `{ report, visualizations }`
  - `report`: `{ id, title, theme }`
  - `visualizations`: array of `{ id, title, view, rows, columns, dataModel }`
  - Always handle the `null` (loading) state before accessing data
  - **DEFENSIVE CODING**: Row values, column fields, and nested properties can be `null`/`undefined`. ALWAYS guard before calling string methods like `.includes()`, `.toLowerCase()`, `.startsWith()`, etc. Use optional chaining (`?.`) or convert first: `String(val || '')`. Example: `(row.name || '').includes('x')` instead of `row.name.includes('x')`.

• **useFilters()** — Global React hook for cross-visualization filtering
  - Returns `{ filters, setFilter, resetFilters, filterRows }`
  - `filters`: current filter state object `{ [field]: selectedValue | string[] }`
  - `setFilter(field, value)`: set a filter (pass `null` or `""` to clear). For categorical: pass array of selected values. For search: pass string.
  - `resetFilters()`: clear all active filters
  - `filterRows(rows, fieldMap?)`: returns rows matching active filters. Optional `fieldMap` remaps filter keys to viz-specific column names, e.g. `filterRows(rows, { country: 'CountryName' })`.
    - Array values (from FilterSelect): exact match — row passes if its value is in the array
    - String values (from FilterSearch): case-insensitive substring match
    - `{ from, to }` values (from FilterDateRange): string comparison range — row passes if `from <= value <= to`
  - Filter state is shared globally — `setFilter` in one component updates `filterRows` everywhere
  - Cross-viz safe: if a row does not have the filtered column (after mapping), it passes through unaffected
  - No automatic column detection — YOU choose which columns to filter using `dtype` and `unique_count` from `visualizations[N].columns`

• **Pre-built UI components** — all global, prefer these for speed but build custom components when the design requires it:
  - `<LoadingSpinner size={24} className="" />` — animated spinner
  - `<CustomTooltip />` — dark styled tooltip component (props: active, payload, label)
  - `<KPICard title="" value="" subtitle="" color="#3B82F6" className="" style={{}} />` — stat card. className adds to defaults (bg-white, border, text-slate-900). Use style={{}} for reliable overrides (e.g. style={{ backgroundColor: '#1e293b', color: '#fff' }})
  - `<SectionCard title="" subtitle="" className="" style={{}}>...children...</SectionCard>` — card wrapper. className adds to defaults (bg-white, border, shadow). Use style={{}} for reliable overrides
  - `<FilterSelect label="" options={[]} selected={[]} onChange={fn} searchable={bool} className="" style={{}} />` — multi-select dropdown with checkboxes, portaled to document.body (always renders above other content). Built-in search auto-enabled at 8+ options (override with `searchable` prop). `options`: unique values from viz column. `selected`: `filters[field] || []`. `onChange`: `arr => setFilter(field, arr)`. className replaces default theme (bg-white border-slate-200 text-slate-900) — for dark themes pass className="bg-slate-900 border-slate-700 text-slate-100". style={{}} also supported for overrides.
  - `<FilterSearch label="" value="" onChange={e => setFilter(field, e.target.value)} placeholder="Search..." className="" style={{}} />` — text search input (standard DOM event). Use for columns with mostly unique values (titles, names). className replaces default theme. style={{}} for overrides.
  - `<FilterDateRange label="" value={filters[field] || {}} onChange={val => setFilter(field, val)} type="date" className="" style={{}} />` — from/to date range picker. `value`/`onChange` use `{ from, to }` object. `type`: "date" (default), "month", or "datetime-local". className replaces default theme. style={{}} for overrides.
  - `fmt(n, {currency: true})` — number formatter (currency, pct, auto K/M/B)

• **window.ARTIFACT_DATA** — Raw data object (same shape as useArtifactData return)

The code is rendered into `<div id="root">`.

CUSTOM COMPONENTS — build your own when the user's design requires something the globals don't cover:
- You have full React 18 + Tailwind + ECharts — use them creatively for custom UX (tabs, progress bars, sparklines, custom legends, interactive tables, etc.)
- Custom overlays/dropdowns: use inline `style={{ backgroundColor: '#fff' }}`, `z-50`, `absolute`, and a `mousedown` click-outside listener
- Use `useFilters()` for filter state — call `setFilter(field, value)` to update, `filterRows(rows)` to read. Do NOT duplicate filter state in local component state
""".strip()


# ---------------------------------------------------------------------------
# Observation field: included in read_artifact observations for the planner
# ---------------------------------------------------------------------------

SANDBOX_RUNTIME_OBSERVATION = (
    "This code runs inside a sandboxed iframe that pre-loads these globals — "
    "do NOT redefine, import, or remove references to them: "
    "React (v18), ReactDOM, echarts (v5), Tailwind CSS (v3.4), Babel (JSX transpilation), "
    "useArtifactData() hook (returns { report, visualizations } or null while loading), "
    "useFilters() hook (returns { filters, setFilter, resetFilters, filterRows } "
    "for cross-visualization filtering — no auto column detection, LLM chooses which columns to filter "
    "using dtype and unique_count from viz.columns (e.g. dtype 'object' + unique_count < 50 → FilterSelect, "
    "dtype 'datetime64[ns]' → FilterDateRange, high unique_count → FilterSearch). "
    "filterRows(rows, fieldMap?) supports optional field mapping "
    "for cross-viz column name differences e.g. filterRows(rows, { country: 'CountryName' }). "
    "Array filter values = exact match (FilterSelect), string values = substring search (FilterSearch), "
    "{from,to} values = date range (FilterDateRange)), "
    "<EChart option=... height=N /> wrapper with 'bow' theme (handles init/dispose/resize/styling — supports ALL ECharts chart types including radar, gauge, treemap, funnel, sankey, etc.), "
    "Pre-built globals (prefer for speed, but build custom React components when the design requires it): LoadingSpinner, KPICard (className additive, style prop for overrides), SectionCard (className additive, style prop for overrides), FilterSelect (className replaces default theme 'bg-white border-slate-200 text-slate-900' — pass e.g. 'bg-slate-900 border-slate-700 text-slate-100' for dark, portaled dropdown, built-in search at 8+ options), FilterSearch (className replaces default theme), FilterDateRange (className replaces default theme), fmt(). "
    "Full React 18 + Tailwind + ECharts available for custom components when needed. "
    "The code is wrapped in <script type='text/babel'> and rendered into <div id='root'>. "
    "All globals (React, echarts, EChart, LoadingSpinner, useArtifactData, useFilters, useState, useEffect, useRef, useMemo, useCallback) are always available at runtime. "
    "NEVER destructure hooks from React (e.g. 'const { useState } = React') — Babel standalone cannot parse it. Use hooks directly as globals."
)
