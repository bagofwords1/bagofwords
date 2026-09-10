"""
Shared sandbox runtime context for artifact tools.

Single source of truth describing the iframe sandbox environment where
LLM-generated artifact code executes. Used by create_artifact, edit_artifact,
and read_artifact to ensure the AI never misdiagnoses missing globals or
misinterprets minified React errors.
"""

# Built-in artifact themes (must match THEMES in frontend/public/libs/artifact-globals.js).
ARTIFACT_THEMES = ("ledger", "nocturne", "atelier", "signal", "meadow", "slate", "sunset", "graphite")
# Runtime generation stamped on new artifact rows as content.runtime_version. The
# sandbox globals switch to the themed design system at >= 11; rows without the
# stamp keep the pre-v11 look and semantics forever.
ARTIFACT_RUNTIME_VERSION = 11

# ---------------------------------------------------------------------------
# Prompt section: embedded in LLM prompts for create_artifact & edit_artifact
# ---------------------------------------------------------------------------

SANDBOX_RUNTIME_PROMPT = """
═══════════════════════════════════════════════════════════════════════════════
SANDBOX RUNTIME (pre-loaded globals — never import, redefine or remove them)
═══════════════════════════════════════════════════════════════════════════════
The code runs in a sandboxed iframe with React 18, ECharts 5, Tailwind CSS 3.4 and
Babel already loaded as globals. NEVER write `import` statements (they throw
"Cannot use import statement outside a module" and kill the whole page). Hooks
are globals too: useState, useEffect, useRef, useMemo, useCallback — call EVERY
hook unconditionally at the TOP of a component, before any early return.

THEMES — setTheme(name, overrides?) selects the design system. Call it ONCE at the
top of the script, before `function App()`. It writes the tokens below as CSS
variables; components, Tailwind utilities and charts all derive from them and
adapt to the viewer's light/dark mode automatically.
  Built-in themes (name — mood — display / body faces):
  • ledger   — finance, board reporting, audited and permanent — Fraunces / Inter
  • nocturne — dark-native analytics and live operations, control-room feel — Sora / Geist (always dark)
  • atelier  — music, media, culture, creative products; expressive, editorial — Instrument Serif / Plus Jakarta Sans
  • signal   — operations, monitoring, logistics, incidents; dense and utilitarian — Bricolage Grotesque / Manrope
  • meadow   — health, people, sustainability, education; calm and organic — DM Serif Display / DM Sans
  • slate    — corporate, internal tooling, general reporting; quiet and precise — Outfit / Inter
  • sunset   — retail, consumer, marketing, hospitality; warm and energetic — Sora / Plus Jakarta Sans
  • graphite — engineering, infrastructure, data platforms; monospace numerals, always dark — Geist / IBM Plex Mono
  Overrides (optional second argument) — tune a theme to the subject:
    setTheme('atelier', { accent: '#d63c5e', accent2: '#1f9d6a', fontDisplay: BOW_FONTS.playfair,
                          chart: ['#d63c5e','#1f9d6a','#f2b134','#3f7cf5'], radius: 'sharp' | 'crisp' | 'soft' | 'round',
                          shadow: 'none' | 'soft' | 'deep', mode: 'dark' })
    Custom palette: setTheme({ extends: 'slate', name: 'brand', light: { bg:'#…', surface:'#…', accent:'#…' }, dark: { … } })
  Vendored faces (BOW_FONTS.*): fraunces, instrument, playfair, dmserif, bricolage, sora, manrope, jakarta, geist, inter, outfit, dmsans, jetbrains, plexmono.
  useTheme() → { name, dark, colors: { bg, surface, surface2, line, line2, ink, ink2, ink3, accent, accent2, accentInk, positive, warning, negative, chart:[8] }, fonts } — for inline styles and chart options that need a literal color.

TOKEN UTILITIES (Tailwind classes that follow the theme; prefer these over raw colors):
  Colors   bg-bg bg-surface bg-surface-2 bg-accent bg-accent-2 bg-ink · text-ink text-ink-2 text-ink-3 text-accent text-accent-ink text-bg
           border-line border-line-2 border-accent · text-positive text-warning text-negative (semantic, never decorative)
           bg-chart-1 … bg-chart-8 (the chart palette) · opacity modifiers work: bg-accent/10, text-ink/60
  Type     font-display (headings, hero numbers) · font-body (everything else) · font-mono · font-numeric (the theme's numeral face) · tracking-eyebrow
  Shape    rounded-card rounded-control rounded-chip · shadow-card shadow-lift
  Raw hex, slate-*, gray-*, blue-* classes are wrong in a themed artifact: they ignore the theme and break dark mode.

DATA ACCESS:
  const data = useArtifactData();           // null while loading → render <LoadingSpinner/>; then { report, visualizations, files, current_user }
  const sales = vizById("<uuid>");          // ID-KEYED, MANDATORY: copy the uuid from the `id` of each viz in YOUR VISUALIZATIONS. Never viz[N].
  sales.rows                                 // Sample vs full data: the prompt shows a ≤100-row SAMPLE; at runtime `rows` is the FULL dataset (row_count is the true size)
  sales.columns                              // [{ field, headerName, dtype, unique_count }] — read cells as row[column.field]
  Aggregate with reduce/Map; never hardcode values; guard nullish values before string methods: String(v ?? '').

FILTERING & PARAMETERS:
  useFilters() → { filters, setFilter(field, value), resetFilters, filterRows(rows, fieldMap?) } — client-side, over rows already loaded.
    Array value = exact match (FilterSelect), string = substring (FilterSearch), {from,to} = range (FilterDateRange).
    Every viz that shares the filter's column must read filterRows(...); a filter that silently skips a chart is a bug.
  useParams() → { declarations, values, pending, loading, error, setParam, setParams, apply, refresh } — SERVER-SIDE parameters:
    setParam('<declared name>', value) re-runs the declaring queries at the source; fresh rows arrive through useArtifactData().
    Choices come from useParamOptions(name) (stable, host-resolved) — never from the rows the control filters. Bind option.value, not the label.
    Scalar params: <FilterSelect single selected={[values.name]} onChange={a => setParam('name', a[0] ?? null)} />; list params: multi-select submitting an array; null = All.
    source:'identity' params are locked to the viewer — render a "scoped to you" badge, never an input. Show useParams().loading and ALWAYS render useParams().error.
  Only declared params get controls; `declarations` may be empty.

VIEWER IDENTITY: useCurrentUser() → { id, name, email, image_url, role, profile_attributes, groups } — the VIEWING user, injected per viewer at render time.
  **`current_user` MAY BE `null`** (anonymous viewers, preview renders) and EVERY field inside it may be null — guard everything:
  {u?.name ? `Welcome back, ${u.name}` : 'Welcome back'}. `groups` is a server-capped list of org group names ((u?.groups || []).includes('Sales'));
  `profile_attributes` keys depend on the org. Display-only personalization, never access control (all data is in the payload regardless).
  NEVER hardcode a specific person's name/email anywhere (titles, greetings, filenames), even when the report title carries one — bind to current_user with a neutral fallback.
  RULES OF HOOKS: call every hook (useState, useMemo, useTheme, useFilters, useParams, useCurrentUser …) unconditionally at the top of the component, BEFORE any early return.

FILES: <BowFile id="<file_id>" fit="contain|cover" className="" /> renders an embedded image or PDF by id (ids are listed in the prompt when present).
  Absolutely-positioned children become annotations over the file. Never use a raw <img src> or inline base64.

COMPONENTS (all globals; `className` MERGES with the defaults — layout classes like `lg:col-span-2 h-full` add, while a bg-/text-color/border-color/rounded-/shadow-/padding class REPLACES the default of that kind):
  <PageHeader eyebrow="" title="" subtitle="" size="lg" actions={[…]} />  — the opening: display-face title, one-line thesis as subtitle.
  <KPICard title="" value={fmt(n,{currency:true})} delta={0.184} deltaPct deltaLabel="vs last year" invertDelta spark={[…numbers]} icon="trending-up"
           subtitle="" variant="card|plain|inset|lift|accent|inverse|outline" size="md|lg" viz={vizById("…")} rows={filtered} calc="SUM(x)" />
    Every KPI gets a comparison (delta or subtitle) — a number with no comparison is not information. `delta` is a ratio when deltaPct (0.18 = +18%).
  <SectionCard title="" subtitle="" eyebrow="" actions={[…]} variant="card|plain|inset|lift|accent|inverse|outline" padding="none|sm|md|lg" viz={…} rows={…} calc="">…</SectionCard>
  <EChart height={N} option={{…}} viz={…} rows={…} calc="" palette={[…]} onClick={fn} />  — themed ECharts wrapper; pass `viz` when it is not inside a SectionCard.
  <DataTable viz={…} rows={filtered} columns pageSize={15} density="compact" searchable striped sortable exportable selectable onRowClick={(row)=>…}
             renderCell={(value,row,col)=>node|null} format={(n,col)=>string} maxHeight={400} />  — the default table: sort, paginate, RTL, CSV, print-safe.
  <FilterBar onReset={resetFilters}> <FilterSelect label options selected onChange single searchable placeholder /> <FilterSearch label value onChange placeholder />
             <FilterDateRange label value onChange type="date|month|datetime-local" /> </FilterBar>
  <Segmented options={[{value,label,icon}]} value onChange />   <Badge tone="neutral|accent|positive|warning|negative|inverse|outline" icon dot>…</Badge>
  <Delta value={0.12} ratio invert chip label="" />  (ratio/pct: value is a share, 0.12 → +12.0%)   <Sparkline data={[…]} height={32} color="var(--bow-accent)" area endpoint />
  <ProgressBar value={0.72} tone="accent|positive|warning|negative|ink" />   <Icon name="<lucide name, kebab-case>" size={16} strokeWidth={2} />
  <Eyebrow>Section label</Eyebrow>   <Divider>optional label</Divider>   <EmptyState icon="inbox">No rows match</EmptyState>   <LoadingSpinner size={24} />
  fmt(n, { currency: true | 'EUR', pct: true, ratio: true, decimals, compact: false, sign: true })  ·  exportCSV(rows, { columns, filename })
    pct: n is ALREADY a percentage (35.8 → "35.8%"); ratio: n is a share (0.358 → "35.8%"). A share passed with pct prints "0.4%" — the most common wrong number on a dashboard.
  Icons: any lucide name (trending-up, users, globe, music, disc-3, calendar, filter, alert-triangle, check-circle, clock, map-pin, package, …).

PROVENANCE (required — the ⓘ popover lets readers inspect the data behind every number):
  Pass viz={vizById("…")} (plus rows={…} when filtered and calc="…" when derived) to every KPICard, SectionCard and bare EChart.
  For custom markup, put data-bow-viz="<uuid>" (and data-bow-calc="…") on the item's outer element.
  EVERY metric, chart and table must be reachable one of these two ways. UUIDs live in code only — never in visible text.

CHARTS: <EChart option={{ xAxis:{type:'category',data:[…]}, yAxis:{type:'value'}, series:[{type:'bar',data:[…]}] }} /> — the 'bow' theme sets palette, fonts, axes,
  grid and tooltip; write only the data mapping unless the design needs more. All ECharts types work (line, bar, pie, scatter, heatmap, treemap, sankey, radar, gauge, calendar…).
  Color one series by role with useTheme().colors (e.g. itemStyle:{color: t.colors.accent}); never invent hex colors outside the theme.
""".strip()


# ---------------------------------------------------------------------------
# Observation field: included in read_artifact observations for the planner
# ---------------------------------------------------------------------------

SANDBOX_RUNTIME_OBSERVATION = (
    "This code runs inside a sandboxed iframe that pre-loads these globals — "
    "do NOT redefine, import, or remove references to them: "
    "React (v18), ReactDOM, echarts (v5), Tailwind CSS (v3.4 with theme tokens: bg-surface, text-ink, "
    "text-ink-2/3, text-accent, border-line, font-display/body/mono/numeric, rounded-card/control/chip, "
    "shadow-card/lift, bg-chart-1..8), Babel (JSX transpilation), lucide icons via <Icon name=... />, "
    "setTheme(name, overrides) — themes: ledger, nocturne, atelier, signal, meadow, slate, sunset, graphite "
    "(called once at top level; writes the design tokens and the ECharts 'bow' theme; legacy artifacts "
    "without setTheme render in the pre-v11 slate/blue look), useTheme() (current tokens), "
    "useArtifactData() hook (returns { report, visualizations, files, current_user } or null while loading), "
    "vizById(uuid) (id-keyed data access), "
    "useCurrentUser() hook (the viewing user { id, name, email, image_url, role, profile_attributes, groups } or null for "
    "anonymous viewers/preview renders — injected per viewer at render time, every field nullable, guard all access; "
    "display-only personalization, not access control), "
    "useParams() hook (server-side query parameters: { declarations, values, pending, loading, "
    "error, setParam, setParams, apply, refresh } — setParam re-runs the declaring queries at the "
    "data source and fresh rows arrive via useArtifactData; identity-source params are locked to "
    "the viewer, render a 'scoped to you' badge, never an input), "
    "useParamOptions(name) hook (stable [{value,label}] choices for a declared param), "
    "useFilters() hook (returns { filters, setFilter, resetFilters, filterRows } for client-side cross-visualization "
    "filtering; filterRows(rows, fieldMap?) remaps column names), "
    "<EChart option=... height=N viz=... /> (themed ECharts wrapper — all chart types), "
    "components: PageHeader, KPICard (title, value, delta, deltaPct, deltaLabel, spark, icon, variant, size), "
    "SectionCard (title, subtitle, eyebrow, actions, variant, padding), DataTable (the default table renderer: sort, "
    "paginate, RTL, CSV, print-safe), FilterBar, FilterSelect (single/multi, searchable), FilterSearch, FilterDateRange, "
    "Segmented, Badge, Delta, Sparkline, ProgressBar, Icon, Eyebrow, Divider, EmptyState, LoadingSpinner, BowFile, "
    "fmt(), exportCSV(). className MERGES with component defaults (layout classes add; a bg-/text-color/border-color/"
    "rounded-/shadow-/padding class replaces the default of that kind). "
    "The code is wrapped in <script type='text/babel'> and rendered into <div id='root'>. "
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
