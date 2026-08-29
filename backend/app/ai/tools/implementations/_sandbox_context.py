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
  - **RULES OF HOOKS**: call EVERY hook (`useState`, `useMemo`, `useCurrentUser`, `useFilters`, ...) unconditionally at the TOP of the component, BEFORE any early return like `if (!data) return <LoadingSpinner/>`. A hook placed after that return crashes with "Rendered more hooks than during the previous render" in runtimes where data arrives after the first render. Compute derived values with safe defaults instead: `const rows = data?.visualizations?.[0]?.rows || []` above the early return.

• **useCurrentUser()** — Global React hook for the viewing user
  - Shortcut for `useArtifactData()?.current_user`. Returns `{ id, name, email, image_url, role, profile_attributes, groups }` or `null`.
  - **`current_user` MAY BE `null`** (anonymous public viewers, preview renders) and EVERY field inside it may be `null`. ALWAYS guard: `Hello{u?.name ? ', ' + u.name : ''}` — NEVER `u.name` bare. The artifact must render perfectly with `current_user = null`.
  - `role` is the user's organization role (e.g. 'admin', 'member'); `profile_attributes` is a free-form object of identity-provider attributes (e.g. jobTitle, department) whose keys DEPEND ON THE ORG — never assume a key exists.
  - `groups`: array of the viewer's org group NAMES (alphabetical, server-capped — a member of many groups sees a truncated list). May be `[]`, missing, or `null` — guard with `(u?.groups || []).includes('Sales')`. Group names are org-defined; only reference a group by name if the user's request names it.
  - Use for DISPLAY-ONLY personalization: greetings ("Welcome back, Yochay"), showing the viewer's role/department/groups, group- or role-conditional sections, tailoring copy. It is NOT access control — all data in ARTIFACT_DATA is present regardless, so never claim to hide/protect data with it.
  - **NEVER hardcode a specific person's name/email into the artifact** — not in titles, headers, greetings, or labels, even when the report title or conversation contains one (e.g. a report named "Yochay's Album Catalog"). Bind it: `{u?.name ? u.name + "'s " : ''}Album Catalog`. Hardcoded names show the wrong name to every other viewer.
  - **When the request is to RESTRICT DATA by viewer** ("each person sees their own tasks"), do NOT simulate it by filtering rows with `current_user` — that ships everyone's rows to every browser. Data-level identity scoping is done server-side via identity-source query parameters (see useParams); if the queries don't declare one, the data is not per-viewer and the artifact must not pretend it is.
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

• **useParams()** — Global React hook for SERVER-SIDE query parameters (different from useFilters, which only filters rows already in the browser: params RE-RUN the underlying queries at the data source and fresh rows arrive automatically)
  - Returns `{ declarations, values, pending, loading, error, setParam, setParams, apply, refresh }`
  - `declarations`: array of `{ name, type, label, source, default, required, options, query_ids }` — the parameters the report's queries declare, aggregated by name (`query_ids` = which queries a control drives). MAY BE EMPTY — only render param controls for declared params.
  - `values`: current applied values by name. `pending`: staged-but-uncommitted changes. `loading`: true while queries re-run. `error`: last run error or null.
  - `setParam(name, value)`: stage + auto-apply (debounced ~250ms). The host re-runs every query declaring that param and pushes fresh rows into `useArtifactData()` — components re-render automatically; show a subtle loading state while `loading` is true.
  - `setParam(name, value, { apply: false })` / `setParams({a, b}, { apply: false })`: stage only (for an explicit Apply-button pattern — use it when 3+ interdependent controls or heavy queries).
  - `apply()` commits all pending; `apply(['<query_id>'])` commits to specific queries only. `refresh()` / `refresh(['<query_id>'])` force re-runs with current values (wire to a per-widget refresh button when it aids trust).
  - Pass `null` as a value for an optional param to mean "All" (the query skips that predicate).
  - `source: 'identity'` params are LOCKED to the viewing user (server-resolved). NEVER render an input for them — show a small "scoped to you" badge instead. `source: 'input_identity_default'` params open on the viewer's value but ARE editable.
  - Param controls: reuse `<FilterSelect>` (single/multi), `<FilterDateRange>`, or plain inputs, wired to `setParam` instead of `setFilter`. Populate choices with `useParamOptions(name)` (below) — NEVER derive a control's choices from the rows that control filters (selecting a value would collapse the list to the current selection).

• **useParamOptions(name)** — Global React hook: the STABLE choice list for one declared param, or `null` when none is declared. Returns `[{ value, label }]` resolved by the host from the param's declaration (a static `options` list, or an options-source query — the filter-space pattern where e.g. a 'Genres' query feeds the genre control of 'Albums by Genre'). Always use it for select-style param controls; bind `option.value` into `setParam` — NEVER the label (values are often ids; labels are display names). A `list`-typed param needs a multi-select submitting an ARRAY of values (empty = null = All). Every param control MUST call `useParams().setParam('<declared name>', value)` with the exact declared name — local React state alone never re-runs the data — and the page must render `useParams().error` when set.
  - Chart drill-down that must RE-QUERY (change granularity/scope) = `setParams({...})` with the clicked value(s). Drill-down that only narrows rows already loaded = `useFilters().setFilter` (instant, no server trip). Choose per interaction.

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
    "useParams() hook (server-side query parameters: { declarations, values, pending, loading, "
    "error, setParam, setParams, apply, refresh } — setParam re-runs the declaring queries at the "
    "data source and fresh rows arrive via useArtifactData; identity-source params are locked to "
    "the viewer, render a 'scoped to you' badge, never an input), "
    "useParamOptions(name) hook (stable [{value,label}] choices for a declared param — resolved "
    "host-side from static options or an options-source query; never derive choices from the "
    "filtered rows), "
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


# ---------------------------------------------------------------------------
# Identity context: injected into create/edit artifact generation prompts
# (page mode) so the model can resolve identity intent against the org's
# real vocabulary instead of guessing group names / attribute keys.
# ---------------------------------------------------------------------------

# The requester's example object shows the SHAPE; a handful of groups is
# plenty for that (the runtime payload itself is capped separately).
IDENTITY_EXAMPLE_MAX_GROUPS = 10
# Org-wide group names let the model match groups the requester isn't in
# (e.g. "show this section to Finance"). Capped — never the full directory.
IDENTITY_ORG_MAX_GROUPS = 30


async def build_identity_context(db, user, organization) -> str:
    """Prompt section describing viewer identity for this org.

    Contains the requesting user's own current_user object as ONE EXAMPLE
    (their data, their generation request — same trust boundary as the rest
    of the planner context) plus the org's group-name vocabulary. Values must
    never be hardcoded by the model — the section says so explicitly, and the
    validation render (current_user=null) surfaces baked-in names.

    Returns "" on any failure or missing context: identity flavor must never
    break artifact generation.
    """
    if db is None or user is None or organization is None:
        return ""
    try:
        import json as _json

        from sqlalchemy import select

        from app.models.group import Group
        from app.models.group_membership import GroupMembership
        from app.models.membership import Membership

        result = await db.execute(
            select(Membership).where(
                Membership.user_id == str(user.id),
                Membership.organization_id == str(organization.id),
            )
        )
        membership = result.scalars().first()

        result = await db.execute(
            select(Group.name)
            .join(GroupMembership, GroupMembership.group_id == Group.id)
            .where(
                GroupMembership.user_id == str(user.id),
                Group.organization_id == str(organization.id),
            )
            .order_by(Group.name)
            .limit(IDENTITY_EXAMPLE_MAX_GROUPS)
        )
        user_groups = [r[0] for r in result.all()]

        result = await db.execute(
            select(Group.name)
            .where(Group.organization_id == str(organization.id))
            .order_by(Group.name)
            .limit(IDENTITY_ORG_MAX_GROUPS)
        )
        org_groups = [r[0] for r in result.all()]

        example = {
            "id": str(user.id),
            "name": getattr(user, "name", None),
            "email": getattr(user, "email", None),
            "role": membership.role if membership else None,
            "profile_attributes": (membership.profile_attributes if membership else None) or None,
            "groups": user_groups,
        }
        org_groups_line = (
            f"\nOrg groups (first {IDENTITY_ORG_MAX_GROUPS}): {', '.join(org_groups)}"
            if org_groups
            else ""
        )
        return f"""
**Viewer identity (current_user):** The requesting user's own object, as ONE EXAMPLE — every viewer gets their own values at render time, and anonymous viewers get `null`:
{_json.dumps(example, default=str)}{org_groups_line}
Use the example ONLY to learn the shape and which keys/groups exist in this org. NEVER hardcode these values into the artifact — always read `useCurrentUser()` at runtime (`u?.name`, `u?.groups`) so each viewer sees their own version, and keep every access null-guarded.
NO BAKED SPECIFICS — this applies to EVERY person-specific literal, wherever it comes from: the report title, the conversation, or existing code. A title like "Yochay's Album Catalog" is personalization leaking into a shared artifact — render it as `{{u?.name ? u.name + "'s " : ''}}Album Catalog` (dynamic with a neutral fallback), never as a hardcoded name and never by silently deleting the personalization. When the user asks to make a name "dynamic", BIND it to current_user — removing it entirely does not satisfy the request. The same applies to titles/headings/labels YOU invent inside the code (page headers, tab labels, export filenames): keep them viewer-agnostic or current_user-bound, never a specific person's name.
Group checks are EXACT, case-sensitive string matches against the names above. When the user refers to a group loosely ("the leadership team", "managers"), find the closest name in the org groups list and use that EXACT string (e.g. `(u?.groups || []).includes('Leadership')`) — never the user's paraphrase. Only use a name outside the list if the user typed it verbatim and nothing in the list plausibly matches."""
    except Exception:
        return ""


# Appended wherever a validation/preview screenshot is attached for planner
# reflection. The preview deliberately renders as an ANONYMOUS viewer, and
# without this label the planner reads the neutral fallbacks as a defect and
# "repairs" working personalization by hardcoding the requester's name.
ANON_PREVIEW_NOTE = (
    " NOTE: this preview renders as an ANONYMOUS viewer (current_user = null), "
    "so personalized content (greetings, viewer names, group-conditional "
    "sections) correctly shows its neutral fallbacks here. A missing name in "
    "this screenshot is EXPECTED behavior, not a bug — at view time each "
    "signed-in viewer sees their own identity. NEVER 'fix' absent "
    "personalization by hardcoding a specific person's name."
)


# Appended wherever a validation/preview screenshot is attached. The image is a
# STATIC capture at page load with nothing clicked, so every closed-by-default
# UI surface is absent from it. Without this label the planner reads that
# absence as a defect and burns its edit budget "fixing" filters that work —
# the observed failure was a multi-select whose options are only in the DOM
# once the dropdown is opened.
STATIC_PREVIEW_NOTE = (
    " NOTE: this is a STATIC screenshot taken at page load — NOTHING has been "
    "clicked, typed into, hovered or focused. Dropdowns, multi-selects, "
    "comboboxes, popovers, modals, tooltips and accordions therefore render in "
    "their CLOSED state, and their options/menu items are correctly absent from "
    "this image. Not seeing a control's options here is EXPECTED and is NEVER "
    "evidence that the control is broken or unpopulated. Do NOT 'fix' a filter, "
    "menu or dropdown because its choices are not visible in the screenshot — "
    "this image cannot show interactive behavior at all. To check such a "
    "control, read its wiring in the CODE (does it map over its options and set "
    "state?); if the code is correct, it works. Only defects visible in a "
    "resting page — layout breakage, wrong or missing values, unreadable "
    "contrast, error text — are diagnosable from this screenshot."
)
