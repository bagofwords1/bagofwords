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
  - Use the `<EChart>` wrapper component (see below) — do NOT use raw echarts.init/dispose
  - Full ECharts option API: bar, line, pie, scatter, radar, treemap, heatmap, gauge, etc.

• **`<EChart>`** — Global React wrapper for ECharts (handles init/dispose/resize)
  - Props: `option` (ECharts option object), `height` (number, default 400), `className` (string)
  - Usage: `<EChart height={400} option={{ xAxis: {...}, series: [...] }} />`
  - NO useRef, NO useEffect, NO echarts.init — just pass the option object
  - Auto-resizes via ResizeObserver
  - Uses 'bow' theme: colors, tooltip, grid, axis styling, rounded corners all pre-configured
  - Only specify data mapping in option — do NOT repeat styling the theme provides

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
  - No automatic column detection — YOU choose which columns to filter by inspecting `visualizations[N].columns` and `visualizations[N].rows`

• **Pre-built UI components** — all global, do NOT redefine:
  - `<LoadingSpinner size={24} className="" />` — animated spinner
  - `<CustomTooltip />` — dark styled tooltip component (props: active, payload, label)
  - `<KPICard title="" value="" subtitle="" color="#3B82F6" className="bg-white border-slate-200 text-slate-900" titleClassName="text-slate-500" />` — stat card. className replaces default theme colors (no className = light mode)
  - `<SectionCard title="" subtitle="" className="bg-white border-slate-200" titleClassName="text-slate-800">...children...</SectionCard>` — card wrapper. className replaces default theme (no className = light mode)
  - `<FilterSelect label="" options={[]} selected={[]} onChange={fn} searchable={bool} />` — multi-select dropdown with checkboxes. Built-in search auto-enabled at 8+ options (override with `searchable` prop). `options`: unique values from viz column. `selected`: `filters[field] || []`. `onChange`: `arr => setFilter(field, arr)`.
  - `<FilterSearch label="" value="" onChange={e => setFilter(field, e.target.value)} placeholder="Search..." />` — text search input (standard DOM event). Use for columns with mostly unique values (titles, names).
  - `<FilterDateRange label="" value={filters[field] || {}} onChange={val => setFilter(field, val)} type="date" />` — from/to date range picker. `value`/`onChange` use `{ from, to }` object. `type`: "date" (default), "month", or "datetime-local".
  - `fmt(n, {currency: true})` — number formatter (currency, pct, auto K/M/B)

• **window.ARTIFACT_DATA** — Raw data object (same shape as useArtifactData return)

The code is rendered into `<div id="root">`.

CUSTOM OVERLAY/DROPDOWN COMPONENTS (only if globals don't cover your need):
- Always use inline `style={{ backgroundColor: '#fff' }}` on dropdown/overlay panels
- Always use `z-50` + `position: absolute` (or fixed for modals)
- Always add click-outside-to-close via `mousedown` listener on `document`
- Use `useFilters()` for filter state — call `setFilter(field, value)` to update, `filterRows(rows)` to read
- Do NOT duplicate filter state in local component state
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
    "by inspecting viz.columns/rows. filterRows(rows, fieldMap?) supports optional field mapping "
    "for cross-viz column name differences e.g. filterRows(rows, { country: 'CountryName' }). "
    "Array filter values = exact match (FilterSelect), string values = substring search (FilterSearch), "
    "{from,to} values = date range (FilterDateRange)), "
    "<EChart option=... height=N /> wrapper with 'bow' theme (handles init/dispose/resize/styling — do NOT use raw echarts.init, do NOT repeat theme styling), "
    "Pre-built globals (do NOT redefine): LoadingSpinner, KPICard, SectionCard, FilterSelect (with built-in search at 8+ options), FilterSearch, FilterDateRange, fmt(). "
    "For charts, use <EChart> wrapper — it is fast and eliminates lifecycle bugs. "
    "The code is wrapped in <script type='text/babel'> and rendered into <div id='root'>. "
    "All globals (React, echarts, EChart, LoadingSpinner, useArtifactData, useFilters, useState, useEffect, useRef, useMemo, useCallback) are always available at runtime. "
    "NEVER destructure hooks from React (e.g. 'const { useState } = React') — Babel standalone cannot parse it. Use hooks directly as globals."
)
