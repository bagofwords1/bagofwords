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

• **Recharts** — All components are pre-loaded as globals (kept for backward compatibility)
  - Available globally: BarChart, Bar, LineChart, Line, etc.
  - Existing Recharts artifacts will continue to work

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
  - Returns `{ filterableColumns, filters, setFilter, resetFilters, filterRows }`
  - `filterableColumns`: auto-detected categorical columns (array of `{ field, unique_values }`)
  - `filters`: current filter state object `{ [field]: selectedValue }`
  - `setFilter(field, value)`: set a filter (pass `null` or `""` to clear)
  - `resetFilters()`: clear all active filters
  - `filterRows(rows)`: returns rows matching active filters — call on each viz's rows before rendering
  - Filter state is shared globally — `setFilter` in one component updates `filterRows` everywhere
  - Columns with only numeric values, fewer than 2 unique values, or more than 30 unique values are excluded
  - Cross-viz safe: if a row does not have a filtered field, it passes through unaffected

• **Pre-built UI components** — all global, do NOT redefine:
  - `<LoadingSpinner size={24} className="" />` — animated spinner
  - `<CustomTooltip />` — dark styled Recharts tooltip. Use: `<Tooltip content={<CustomTooltip />} />`
  - `<KPICard title="" value="" subtitle="" color="#3B82F6" className="bg-white border-slate-200 text-slate-900" titleClassName="text-slate-500" />` — stat card. className replaces default theme colors (no className = light mode)
  - `<SectionCard title="" subtitle="" className="bg-white border-slate-200" titleClassName="text-slate-800">...children...</SectionCard>` — card wrapper. className replaces default theme (no className = light mode)
  - `<FilterSelect label="" options={[]} selected={[]} onChange={fn} className="" />` — multi-select dropdown with checkboxes. className replaces default theme. Use ONLY for categorical filters (country, genre, status, etc.). For date ranges, numeric ranges, or search inputs, build a custom component using standard HTML inputs — do NOT force FilterSelect for non-categorical data.
  - `fmt(n, {currency: true})` — number formatter (currency, pct, auto K/M/B)

• **window.ARTIFACT_DATA** — Raw data object (same shape as useArtifactData return)

The code is rendered into `<div id="root">`.
""".strip()


# ---------------------------------------------------------------------------
# Observation field: included in read_artifact observations for the planner
# ---------------------------------------------------------------------------

SANDBOX_RUNTIME_OBSERVATION = (
    "This code runs inside a sandboxed iframe that pre-loads these globals — "
    "do NOT redefine, import, or remove references to them: "
    "React (v18), ReactDOM, echarts (v5), Tailwind CSS (v3.4), Babel (JSX transpilation), "
    "useArtifactData() hook (returns { report, visualizations } or null while loading), "
    "useFilters() hook (returns { filterableColumns, filters, setFilter, resetFilters, filterRows } "
    "for cross-visualization filtering with shared state), "
    "<EChart option=... height=N /> wrapper with 'bow' theme (handles init/dispose/resize/styling — do NOT use raw echarts.init, do NOT repeat theme styling), "
    "Pre-built globals (do NOT redefine): LoadingSpinner, KPICard, SectionCard, FilterSelect, fmt(). "
    "Recharts is also available globally for backward compat. "
    "For NEW dashboards, use <EChart> wrapper — it is fast and eliminates lifecycle bugs. "
    "The code is wrapped in <script type='text/babel'> and rendered into <div id='root'>. "
    "All globals (React, echarts, EChart, LoadingSpinner, useArtifactData, useFilters, useState, useEffect, useRef, useMemo, useCallback) are always available at runtime. "
    "NEVER destructure hooks from React (e.g. 'const { useState } = React') — Babel standalone cannot parse it. Use hooks directly as globals."
)
