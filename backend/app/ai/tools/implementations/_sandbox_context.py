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
references to any of them.

⚠️  NEVER write `import` statements of any kind. They cause an immediate
    SyntaxError: "Cannot use import statement outside a module" that breaks
    the entire dashboard. All globals below are already available — use them
    directly with no imports.

• **React 18** — `React`, `ReactDOM` available globally
  - Hooks are also global: `useState`, `useEffect`, `useRef`, `useMemo`, `useCallback` — use directly without `React.` prefix

• **ECharts 5** — `echarts` available globally
  - Prefer the `<EChart>` wrapper component (see below) for standard charts
  - Full ECharts option API supported: bar, line, pie, scatter, radar, treemap, sunburst, heatmap, gauge, funnel, sankey, parallel, calendar, graph, etc.

• **`<EChart>`** — Global React wrapper for ECharts (handles init/dispose/resize)
  - Props: `option` (ECharts option object), `height` (number, default 400), `className` (string), and optional `viz`/`rows`/`calc`
  - Usage: `<EChart height={400} option={{ xAxis: {...}, series: [...] }} />`
  - When a chart is NOT wrapped in a SectionCard, pass `viz={visualizations[N]}` (and `rows`/`calc` if applicable) directly to `<EChart>` so it still shows the built-in "ⓘ" info popover.
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
  - Returns `null` while data is loading, then `{ report, visualizations, files, current_user }`
  - `report`: `{ id, title, theme }`
  - `visualizations`: array of `{ id, title, view, rows, columns, dataModel }`
  - `files`: array of `{ id, content_type, filename }` — embedded images/PDFs; render each with `<BowFile id={...} />`
  - `current_user`: the VIEWING user, or `null` — `{ id, name, email, image_url, role, profile_attributes, groups }`. Injected fresh per viewer at render time (never baked into the artifact), so the same dashboard can greet each viewer personally.
  - Always handle the `null` (loading) state before accessing data

• **useCurrentUser()** — Global React hook for the viewing user
  - Shortcut for `useArtifactData()?.current_user`. Returns `{ id, name, email, image_url, role, profile_attributes, groups }` or `null`.
  - **`current_user` MAY BE `null`** (anonymous public viewers, preview renders) and EVERY field inside it may be `null`. ALWAYS guard: `Hello{u?.name ? ', ' + u.name : ''}` — NEVER `u.name` bare. The artifact must render perfectly with `current_user = null`.
  - `role` is the user's organization role (e.g. 'admin', 'member'); `profile_attributes` is a free-form object of identity-provider attributes (e.g. jobTitle, department) whose keys DEPEND ON THE ORG — never assume a key exists.
  - `groups`: array of the viewer's org group NAMES (alphabetical, server-capped — a member of many groups sees a truncated list). May be `[]`, missing, or `null` — guard with `(u?.groups || []).includes('Sales')`. Group names are org-defined; only reference a group by name if the user's request names it.
  - Use for DISPLAY-ONLY personalization: greetings ("Welcome back, Yochay"), showing the viewer's role/department/groups, group- or role-conditional sections, tailoring copy. It is NOT access control — all data in ARTIFACT_DATA is present regardless, so never claim to hide/protect data with it.
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
  - `<KPICard title="" value="" subtitle="" color="#3B82F6" viz={visualizations[N]} className="" style={{}} />` — stat card. className adds to defaults (bg-white, border, text-slate-900). Use style={{}} for reliable overrides (e.g. style={{ backgroundColor: '#1e293b', color: '#fff' }})
  - `<SectionCard title="" subtitle="" viz={visualizations[N]} className="" style={{}}>...children...</SectionCard>` — card wrapper. className adds to defaults (bg-white, border, shadow). Use style={{}} for reliable overrides
  - `<DataTable viz={visualizations[N]} />` — **the default way to render a table.** Full-featured out of the box: click-to-sort headers (numeric-aware), pagination (default 15 rows/page), row hover + click-to-highlight, numeric columns right-aligned & formatted, null → "—", CSV download button, automatic RTL (Hebrew/Arabic data flips the table direction; cells are dir="auto" for mixed text), sticky header, and print/PDF-safe (all rows print even when paginated on screen). ALWAYS prefer it over hand-rolled `<table>` markup; wrap it in `<SectionCard viz={...}>` for the title + ⓘ popover. Optional props: `rows={filteredRows}` (pass `filterRows(...)` output — keeps filters working), `columns` (override viz.columns), `pageSize={25}` (0 disables pagination → scrolling table), `searchable` (adds a quick-search box), `sortable={false}`, `exportable={false}` (hides the CSV button), `selectable={false}` (disables click-to-highlight), `striped` (zebra rows), `onRowClick={(row, i) => ...}` (e.g. call `setFilter` for cross-viz drill-down), `maxHeight={400}`, `dir="rtl"|"ltr"` (override auto-detection), `info` (adds its own ⓘ — only when NOT inside a SectionCard), `calc=""`, `className`/`style`.
  - **`viz` prop (KPICard & SectionCard)** — pass the source visualization object and the card automatically shows a small "ⓘ" button that opens a clean popover with that viz's provenance (a Data tab with the rows, a Code tab with the query, plus source/columns/row count/aggregation/id). ALWAYS pass `viz={visualizations[N]}` for the visualization a card is built from. For a card derived from multiple vizs, pass its primary one. No extra markup needed — the popover is built in.
  - **`rows` prop (filter-aware popover)** — when a card renders FILTERED rows (i.e. you computed `filterRows(visualizations[N].rows)` for it), ALSO pass those same rows: `rows={filteredRows}`. The popover's Data tab then shows exactly what the component displays (and labels it "X of Y rows (filtered)") instead of the full unfiltered dataset. If a card uses the raw rows unchanged, omit `rows` — the popover falls back to the full dataset.
  - **`calc` prop (calculation/formula)** — when a card aggregates or DERIVES its value(s) client-side (a `reduce`, group-by, ratio, count-distinct, etc.), pass a short `calc` describing the math with REAL column names, e.g. `calc="SUM(UnitPrice × Quantity) grouped by GenreName"`, `calc="COUNT(DISTINCT CustomerId)"`, or `calc="AVG(Total) where Country = selected"`. The popover surfaces it as a "Calculation" line so users see how the displayed number was computed. Omit it for cards that show raw values unchanged.

• **PER-ITEM INFO ON CUSTOM MARKUP (`data-bow-*` attributes)** — IMPORTANT for custom dashboards.
  When you build your OWN containers (custom `<div>` KPI tiles, chart wrappers, tables) instead of `<KPICard>`/`<SectionCard>`/`<EChart>`, those don't get the built-in "ⓘ" popover. To give every item its popover anyway, annotate the item's outermost element with data attributes — keep your exact design, just add the attributes:
  - `data-bow-viz="N"` — index of the source visualization the item is derived from (required to enable the ⓘ).
  - `data-bow-calc="<formula>"` — the calculation, e.g. `data-bow-calc="SUM(UnitPrice × Quantity) grouped by GenreName"`.
  - `data-bow-title="<label>"` — optional title shown in the popover header (defaults to the viz title).
  Example: `<div data-bow-viz="0" data-bow-calc="SUM(UnitPrice × Quantity)"><span>Total Revenue</span><span>{fmt(total)}</span></div>`.
  A global overlay reads these attributes and renders the same Data/Code/Calc popover at each item's corner. ALWAYS add `data-bow-viz` (and `data-bow-calc` when the value is derived) to EVERY metric tile, chart container, and table you build with custom markup.
  - `<FilterSelect label="" options={[]} selected={[]} onChange={fn} searchable={bool} className="" style={{}} />` — multi-select dropdown with checkboxes, portaled to document.body (always renders above other content). Built-in search auto-enabled at 8+ options (override with `searchable` prop). `options`: unique values from viz column. `selected`: `filters[field] || []`. `onChange`: `arr => setFilter(field, arr)`. className replaces default theme (bg-white border-slate-200 text-slate-900) — for dark themes pass className="bg-slate-900 border-slate-700 text-slate-100". style={{}} also supported for overrides.
  - `<FilterSearch label="" value="" onChange={e => setFilter(field, e.target.value)} placeholder="Search..." className="" style={{}} />` — text search input (standard DOM event). Use for columns with mostly unique values (titles, names). className replaces default theme. style={{}} for overrides.
  - `<FilterDateRange label="" value={filters[field] || {}} onChange={val => setFilter(field, val)} type="date" className="" style={{}} />` — from/to date range picker. `value`/`onChange` use `{ from, to }` object. `type`: "date" (default), "month", or "datetime-local". className replaces default theme. style={{}} for overrides.
  - `<BowFile id="<file_id>" className="" style={{}} alt="" fit="contain|cover" />` — renders an embedded FILE by its id (generated images from generate_image, or uploaded images/PDFs). It auto-detects type from the file: images render as a picture, PDFs render in a scrollable viewer. The bytes are injected at runtime (no auth/URL handling needed) — just pass the `id` from the file list given in the prompt. NEVER use a raw `<img src>` or inline base64 for these; always use `<BowFile>`.
    - ANNOTATIONS: `<BowFile>` accepts children, absolutely positioned over the file, for callouts/highlights. Use percentage coordinates so they scale, e.g. `<BowFile id="..."><div className="absolute" style={{ left: '40%', top: '25%' }}><span className="bg-yellow-300/80 rounded px-1 text-xs">Peak</span></div></BowFile>`.
    - `useArtifactData()` also returns `files` (array of `{ id, content_type, filename }`) so you can list/iterate embedded files if needed.
  - `fmt(n, {currency: true})` — number formatter (currency, pct, auto K/M/B)
  - `exportCSV(rows, { columns, filename })` — trigger client-side CSV download. `rows`: array of objects (e.g. `viz.rows` or `filterRows(viz.rows)`). `columns` (optional): either `viz.columns` or a string array of keys; defaults to `Object.keys(rows[0])`. `filename` (optional): defaults to `'export.csv'`, `.csv` appended if missing. Handles RFC 4180 quoting, `null`/`undefined` → empty cell, objects → JSON, UTF-8 BOM for Excel. Wire to any `onClick` — e.g. `<button onClick={() => exportCSV(filterRows(viz.rows), { columns: viz.columns, filename: viz.title })}>Download CSV</button>`. NOTE: every ⓘ info popover's Data tab and every `<DataTable>` already have a built-in CSV download — only add a SEPARATE export button when the user explicitly asks for one.

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
    "useArtifactData() hook (returns { report, visualizations, files, current_user } or null while loading), "
    "useCurrentUser() hook (the viewing user { id, name, email, image_url, role, profile_attributes, groups } or null for "
    "anonymous viewers/preview renders — injected per viewer at render time, every field nullable, guard all access; "
    "display-only personalization, not access control), "
    "useFilters() hook (returns { filters, setFilter, resetFilters, filterRows } "
    "for cross-visualization filtering — no auto column detection, LLM chooses which columns to filter "
    "using dtype and unique_count from viz.columns (e.g. dtype 'object' + unique_count < 50 → FilterSelect, "
    "dtype 'datetime64[ns]' → FilterDateRange, high unique_count → FilterSearch). "
    "filterRows(rows, fieldMap?) supports optional field mapping "
    "for cross-viz column name differences e.g. filterRows(rows, { country: 'CountryName' }). "
    "Array filter values = exact match (FilterSelect), string values = substring search (FilterSearch), "
    "{from,to} values = date range (FilterDateRange)), "
    "<EChart option=... height=N /> wrapper with 'bow' theme (handles init/dispose/resize/styling — supports ALL ECharts chart types including radar, gauge, treemap, funnel, sankey, etc.), "
    "Pre-built globals (prefer for speed, but build custom React components when the design requires it): LoadingSpinner, KPICard (className additive, style prop for overrides), SectionCard (className additive, style prop for overrides), "
    "DataTable (the DEFAULT table renderer — <DataTable viz={visualizations[N]} /> gives sortable headers, pagination, row hover/selection highlight, numeric alignment/formatting, auto-RTL for Hebrew/Arabic, built-in CSV export, print-safe; props: rows, columns, pageSize, searchable, sortable, exportable, selectable, striped, onRowClick, maxHeight, dir, info, calc — prefer it over hand-rolled <table> markup), "
    "FilterSelect (className replaces default theme 'bg-white border-slate-200 text-slate-900' — pass e.g. 'bg-slate-900 border-slate-700 text-slate-100' for dark, portaled dropdown, built-in search at 8+ options), FilterSearch (className replaces default theme), FilterDateRange (className replaces default theme), fmt(), exportCSV(rows, {columns, filename}) for client-side CSV download (the ⓘ popover Data tab and DataTable already include one built in). "
    "Full React 18 + Tailwind + ECharts available for custom components when needed. "
    "The code is wrapped in <script type='text/babel'> and rendered into <div id='root'>. "
    "All globals (React, echarts, EChart, LoadingSpinner, useArtifactData, useFilters, useState, useEffect, useRef, useMemo, useCallback) are always available at runtime. "
    "NEVER destructure hooks from React (e.g. 'const { useState } = React') — Babel standalone cannot parse it. Use hooks directly as globals."
)
