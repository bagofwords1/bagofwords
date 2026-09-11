# Feedback loop — artifact design system (themed runtime v11 + design plan)

Verifies `docs/design/artifact-design-system.md` against a full local stack:
backend (sqlite, no reloader) + Nuxt dev frontend + real LLMs (Claude 4.5
Haiku and Claude Sonnet 5, each pinned per run via `prompt.model_id`) + the
bundled Chinook SQLite source, driven through `POST /reports/{id}/completions`
and screenshotted from the real `/reports/{id}` pane with Playwright at
1600px (the artifact iframe is ~934px wide there — the width the layout
recipes are calibrated for).

## Method

Three prompts × two models, run BEFORE (commit `7d90eab`, the unmodified
runtime and reference) and AFTER, on the same data source:

1. Sales — monthly revenue trend, revenue by genre and country, top 10
   customers, a country filter driving every chart.
2. Catalog — tracks per genre, average track length, most prolific artists,
   longest albums, "make it feel like a music product, not a finance report".
3. Support reps — revenue and customers per rep, monthly revenue per rep.

The screenshot is the full rendered iframe document. The comparison contact
sheet (all pairs, before/after side by side) is the deliverable of this loop.

## Sandbox notes (beyond the ones in the sandbox-feedback-loop skill)

- `POST /completions` without streaming can return before the agent run is
  over; polling the completions list must wait until nothing is
  `in_progress` for several consecutive polls, not for the first terminal
  status.
- Editing backend prompt files with uvicorn `--reload` on wedged the reloader
  ("Waiting for background tasks to complete") mid-run; the loop ran the
  backend as `uvicorn main:app` with no reloader and restarted it between
  rounds.
- The frontend serves `public/libs/*` directly in dev, so the new
  `artifact-globals.js`, `artifact-tailwind.js`, `lucide.min.js` and
  `artifact-fonts.css` are picked up without a rebuild; the headless
  validation render reads the freshest copy through `artifact_libs`.

## What the baseline showed

- Every dashboard: `system-ui`, white `rounded-2xl` cards, a gradient accent
  bar, one column of full-width charts, blue everything. Sonnet's catalog
  dashboard chose a dark palette but the same rainbow bars.
- Haiku's catalog dashboard rendered **fabricated data** ("Eternal Tones",
  "Harmonic Echoes", 3,600 tracks, genres Chinook does not have) while
  passing the viz-reference gate, because it copied the uuid into
  `vizById()` and then hardcoded arrays.
- Haiku's support-rep prompt produced no artifact at all in the baseline run.

## What the themed runtime + design plan changed (observed)

- Fonts, tokens and icons load offline in every shell: `document.fonts`
  reports the theme's faces (e.g. Fraunces + Inter for `ledger`, Instrument
  Serif + Plus Jakarta Sans for `atelier`); `lucide` icons render as inline
  SVG; the ECharts theme follows the tokens in light and dark mode.
- Legacy compatibility: the baseline Haiku sales artifact (no runtime stamp)
  re-rendered on the new runtime pixel-identical to its baseline capture
  (same 2348px document, `system-ui`, slate/blue kit, additive className,
  legacy `fmt` rounding kept byte-for-byte).
- The design plan appears in `create_artifact.prompt` on every after-run
  (subject & job, theme + overrides, hero, layout archetype, comparison,
  interaction) and the code follows it — different subjects got different
  themes (sunset / atelier for sales and catalog), different type, different
  layout archetypes (headline+grid, bento).
- Haiku's catalog dashboard now binds the real rows (no fabricated data),
  computes four KPIs from them, and lays out a bento grid.
- Sonnet's sales dashboard: Instrument Serif title, eyebrow, four KPIs each
  with a computed comparison ("35% of filtered revenue", "of 59 total
  customers"), hero trend lifted, the selected country highlighted in the
  country chart, a searchable detail table.

## Iterations during the loop

Each of these was found by looking at a real rendered dashboard, fixed, and
re-run on both models.

1. Round 1 exposed that the layout recipes used `lg:` breakpoints, which
   never fire inside the ~934px report pane (`lg` = 1024px), so two-column
   grids collapsed to one column. The recipes now use `md:` for the primary
   grids and the reference states the real pane width.
2. Haiku hand-rolled `(n/1000).toFixed(1)+'K'` and printed `$0.0K` for
   values under $1000; the reference now names `fmt()` as the only number
   formatter and shows its outputs.
3. Haiku read `data.visualizations[0]` positionally; the design gate now
   rejects positional access on the themed runtime with the id-keyed fix
   named.
4. A React key warning from `FilterSelect`'s search input surfaced in the
   validation console log; fixed in the kit.
5. The reference gained a full exemplar page (headline + grid, KPIs computed
   with reduce/Map, deltas, `variant="lift"` on the hero, two-up breakdowns,
   `md:` grids) — models imitate examples far more reliably than they follow
   rules, Haiku especially.
6. A ranked bar chart over 25 genres stacked its category labels, value
   labels ran together in a two-up panel, and a three-series legend sat on
   top of its plot: the chart theme now sets `hideOverlap` on every axis and
   the `EChart` wrapper reserves grid headroom for a legend.
7. **The most persistent defect, across every round and both models:** a
   proportion computed in code and passed to `fmt`'s `pct` printed as
   "0.3% of the catalog" instead of "34.2%". Naming a `share(part, whole)`
   helper and adding a `ratio` option did not stop it, so `pct` itself became
   share-tolerant (a magnitude at or below 1 is read as a share; `exact: true`
   opts out). The lesson: when both models get the same API wrong, fix the
   API, not the prompt.
8. Tables printed raw database output — `TOTALDURATIONMILLISECONDS` headers
   and `2021-02-06T00:00:00.000` cells. `DataTable` now humanizes
   machine-shaped column names and renders SQL timestamps as dates.
9. Prose rules about chart hygiene were not landing on the smaller model, so
   the reference ends with a five-line CHART CHECKLIST (sorted, trimmed to
   the top 8-12, one color per series, formatted with `fmt`/`share`, fits its
   panel) stated as verifiable properties.

## Results

Final round, all six prompt/model pairs produced a working dashboard on the
first turn (the two rep-dashboard runs take a clarifying follow-up about what
a "support rep" is, which is correct behaviour — the term is not in the
schema). Each subject picked its own theme, type and layout archetype. The
before/after contact sheet is the deliverable of this loop.

## Tests

`backend/tests/unit/test_artifact_design_system.py` (theme registry parity
between prompt and globals, runtime stamp/cache-buster parity, the design
gate on themed vs legacy payloads, positional-access rejection, Tailwind
token mapping, every shell loading the same kit, vendored fonts inlined as
data URIs, no CDN references in the runtime) plus the existing artifact
suites (viewer identity, feedback loop, refs, params wiring, validation
runtime parity, parse gate, read windowing, loop guard).
