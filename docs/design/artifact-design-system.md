# Artifact design system — themed runtime (v11) and the design plan

Status: implemented (runtime v11 + planner reference rewrite). Verification:
`docs/feedback-loops/artifact-design-system.md`.

Trigger: every generated dashboard looked the same — system font, white
`rounded-2xl` cards with a blue gradient bar, one column, blue charts — no
matter the subject, the model, or the request. Nothing like the range Claude
Artifacts or v0 produce for comparable prompts.

## Where the sameness came from

- **The component kit was the design.** `artifact-globals.js` hardcoded
  slate/blue Tailwind classes in every component and a fixed 10-color chart
  palette with no font family. The prompt told the model to "prefer these for
  speed", so most output was the kit with data poured in.
- **The override path was broken.** The authoring reference said `className`
  *replaces* the defaults; the code *appended* it (`bg-white bg-slate-900` —
  a coin flip); the sandbox prompt said the opposite of the reference.
- **Nothing to be creative with.** No webfonts, no Tailwind theme extension,
  no icon set. Four shells declared four different `system-ui` stacks.
- **Guidance was rules, not taste.** ~5,700 words of authoring reference,
  ~325 on design, 30 imperative directives; the style skill
  (`dashboard-theme`) was off by default and its house-style section a
  placeholder.
- **False facts.** The reference claimed the side panel is 360–480px (the
  real default is ~63% of the window) and validation screenshots were
  1280×720, so the model optimized for a canvas it never saw.

## The architecture (unchanged) and what changed around it

The planner still authors every artifact (`create_artifact.code`,
`edit_artifact` ops) and the tools stay mechanical: gates + one validation
render, nothing persisted on failure. That part is the moat and is untouched.
What changed is the *visual layer* the planner writes against and the
*guidance* it writes from.

### 1. A token-driven, themed runtime (`frontend/public/libs/artifact-globals.js`)

- `setTheme(name | spec, overrides)` selects one of eight built-in themes —
  `ledger`, `nocturne`, `atelier`, `signal`, `meadow`, `slate`, `sunset`,
  `graphite` — each pairing a display / body / mono / numeric face with a
  light and a dark palette, a radius scale and a shadow treatment. Overrides
  (`accent`, `accent2`, `chart`, `fontDisplay`, `radius`, `shadow`, `mode`)
  and fully custom specs (`{ extends, light, dark, fonts }`) tune it to the
  subject or a brand. `useTheme()` exposes the resolved tokens.
- Tokens are written as CSS variables (`--bow-bg`, `--bow-ink`,
  `--bow-accent`, `--bow-chart-1…8`, `--bow-font-display`,
  `--bow-radius-lg`…). `artifact-tailwind.js` maps them onto Tailwind
  utilities (`bg-surface`, `text-ink-2`, `border-line`, `font-display`,
  `rounded-card`, `shadow-lift`, `bg-chart-3`, with opacity modifiers), and
  the ECharts `bow` theme is regenerated from the same tokens (palette,
  fonts, axes, tooltip) on every theme or color-mode change.
- Components read tokens only: `PageHeader`, `KPICard` (delta, sparkline,
  icon, variants), `SectionCard` (eyebrow, actions, variants), `DataTable`,
  `FilterBar` + filters, `Segmented`, `Badge`, `Delta`, `Sparkline`,
  `ProgressBar`, `Icon` (lucide), `Eyebrow`, `Divider`, `EmptyState`,
  `BowFile`, the ⓘ provenance popovers. Semantic color (positive / warning /
  negative) is separate from the accent.
- `className` **merges**: layout classes (`lg:col-span-2`, `h-full`) add;
  a class of the same kind as a default (bg, text color, border color,
  rounded, shadow, padding) replaces that default. This is the
  `bowMergeClasses` rule; it resolves the replace-vs-append contradiction.
- The fonts are vendored (14 OFL faces, latin woff2, ~700KB) by
  `scripts/download-vendor-libs.sh`, served from `/libs/fonts/` in the app and
  inlined as data URIs for headless validation, thumbnails, PDF and the
  standalone HTML export. Lucide is vendored the same way. Nothing in the kit
  reaches a CDN at render time, so airgapped / self-hosted deployments render
  identically.

### 2. Backwards compatibility: the runtime generation stamp

New rows carry `content.runtime_version = 11`; every host passes it to the
sandbox as `ARTIFACT_DATA.runtime.version`. Below 11 (or absent) the globals
apply the `legacy` theme (the exact pre-v11 slate/blue look, system font) and
the pre-v11 *additive* `className` semantics, so stored artifacts render as
they always did. `edit_artifact` carries the stamp forward unchanged: a legacy
artifact stays legacy across edits; moving it to the themed kit is an explicit
rebuild. Stored rows are never rewritten.

### 3. The design plan (planner guidance rewrite)

The page authoring reference (`CreateArtifactTool._build_page_system_prompt`)
now opens with a mandatory design plan — subject & job, theme for *this*
subject with overrides, hero, layout archetype (headline+grid, editorial
column, split canvas, control room, bento), comparison for every KPI,
interaction model — written into `create_artifact.prompt` and then built to.
The rest of the reference is taste (not everything is a card; typography
carries the page; titles are findings; the AI-dashboard clichés to avoid;
charts drawn to scale) plus concrete Tailwind layout recipes and the
compacted runtime contract. The width facts are corrected (≈900–1300px in
the app, full width when shared, ~400px on phones) and the validation
screenshot is taken at 1280×900.

### 4. Gates

`design_errors` rides the same contract check as viz references and params
wiring, on payloads stamped ≥ 11 only: the code must call `setTheme(...)`,
and a page styled with a pile of raw `slate-*` / `blue-*` classes is
rejected with the token utilities named. Legacy artifacts are never
retro-failed.

### 5. Skills

`dashboard-theme` is installed by default with a real house style (pick the
theme for the subject, type does the branding, one hero then quiet, titles
state the finding, token utilities only).

## Explicitly not done

- No page model, manifest or fixed grid (still rejected: the constraint was
  the problem, not the freedom).
- No change to React / Tailwind / ECharts. The runtime is shared retroactively;
  a swap would strand every stored artifact for no visual gain.
- The MCP `create_artifact` / `edit_artifact` surface still runs the legacy
  in-tool codegen (no `code` argument from the external caller). Its
  artifacts carry no runtime stamp and render in the legacy look. Moving it to
  the themed kit means giving the MCP prompt the design plan and stamping the
  row — a follow-up.
- Model pinning for artifact authoring (Move 3 of the plan) is not part of
  this change.
