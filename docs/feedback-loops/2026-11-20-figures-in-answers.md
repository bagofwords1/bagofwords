# Feedback Loop — "Because we hid create_data visuals, there are no more inline charts"

Since the run fold, a chart the user asked for is folded behind the steps; the
answer only names it as a source ("Revenue by country" link under the text).
The claim validated here: the agent can place the result itself in the answer,
`{{show:<visualization id>}}`, and the report draws it inline as a clean figure
built from what `create_data` already inferred — with a loading state, a
one-time entrance, and never for a single number.

## Root cause (validated)
- `frontend/composables/useRunActivity.ts` folds `create_data` into the run's
  steps; the answer keeps only citation chips (`useCitations.ts`) and the
  sources row. Nothing in the answer grammar could place a result inline.
- The planner was taught `{{cite:…}}` only
  (`backend/app/ai/agents/planner/prompt_builder_v3.py`, citation rule).

## The change
- **Grammar** — `{{show:<id>}}`, same id rules as `{{cite:…}}`:
  - `frontend/composables/useCitations.ts`: one pass in reading order, so a
    figure and the chips share one numbering (figure 1 is source 1). A shown
    result becomes `<bow-figure source=… number=…>`, placed after the
    paragraph it was named in — even when the model writes it mid-sentence or
    in backticks — and the sentence is tidied where it stood. Single values,
    unknown ids and surfaces without figures drop it. Half-streamed markers
    never flash.
  - `backend/app/ai/citations.py`: every plain-text surface (chat channels,
    scheduled-run summaries, evals, artifact conversations) strips it with the
    line it stood on.
  - Planner rule (web report only, `_figures_enabled`): own line, at most 3,
    each once, never a single number, never inside a sentence.
- **Figure** — `frontend/components/markdown/ChatFigure.vue`, a markstream
  custom node (registered in `plugins/markdown-mermaid-png.ts`, enabled per
  renderer with `customHtmlTags`):
  - Reads the result through `composables/figureModel.ts`: chart type,
    category/measure columns, breakdown and units come from create_data's
    inferred `data_model` / `view`. Bars (categorical), columns (periods or
    breakdowns), lines, tables; any other type uses the existing renderer
    inside the same frame. A breakdown whose groups share no category is drawn
    as one series.
  - Loading: the body takes its final height at once with a skeleton while the
    full rows load (`/api/steps/:id`, once per page); entrance: fade-and-rise
    plus one draw, only the first time a result appears (no re-animation when
    text streams after it, or on scroll).
  - Monochrome Vercel-style frame from theme tokens (light/dark), numbered,
    chart ↔ table toggle, open-in-panel, table search/sort/show-all; mirrors
    with the answer's direction, not the UI locale.
- Wired on `/reports/:id` and the shared conversation `/c/[token]` (no row
  loading or panel there).

## Loop A — deterministic
```bash
cd frontend
node tests/unit/useCitations.mjs   # grammar, numbering, placement, streaming
node tests/unit/figureModel.mjs    # inference -> figure shape, units, no single values
cd ../backend
uv run pytest tests/unit/test_citations.py --db=sqlite   # strip/footnote/prompt rule
```
First run caught a real bug: chips and figures were numbered in two passes,
so a figure that came first got number 2. Fixed with the single pass.

## Loop B — live, real model (Claude Haiku 5.5)
```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --demo
ANTHROPIC_API_KEY=… HAIKU_MODEL_ID=claude-haiku-5-5 uv run python ../tools/agent/setup_haiku_llm.py
cd ../frontend && VIDEO=1 node ../tools/agent/figures_ui_flow.mjs <out-dir>
```
The flow signs in, asks from the home composer, waits for the run, audits the
answer and captures each figure, the tooltip, chart→table, table search/sort,
open-in-panel, a reload, and dark / Hebrew / phone variants.

| Question | Figures drawn | Raw markers | Console errors |
|---|---|---|---|
| Revenue by country + top 10 customers | 1 bars, 2 bars (12 of 24 / 10 rows) | none | 0 |
| Monthly revenue trend | 1 line (60 rows) | none | 0 |
| Total revenue (single value) | none — cited in the sentence | none | 0 |
| Same question in Hebrew | 1 bars, mirrored | none | 0 |

Issues the live loop found and fixed: category/axis labels clipped at the left
edge; a year "breakdown" cutting a monthly line into five disjoint pieces; the
model writing the marker mid-sentence in backticks (left a stray "`)." —
now placed after the paragraph and the sentence tidied; prompt sharpened);
Hebrew mirroring following the UI instead of the answer.

## Regression notes
- The sandbox backend froze (event loop stopped, process alive) three times
  after several minutes of live runs; the first freeze predates any figure
  code. Restarted each time; not investigated further here.
- `locales/*.json` already differ on `onboarding.llm.vertex*` on the base
  branch; the new `figure.*` keys are identical across en/es/he.


## Round 2 — paper style, highlight, actions, channels (2026-11-21)
Design chosen from three mockups (Medium / paper / paper + serif): **paper**.

- **Look** (`ChatFigure.vue`): no frame; caption below ("Figure 1. Revenue by
  billing country, top 8 of 24."); controls are text on hover (always on
  touch): Table/Chart · Open in panel · •••. One muted accent for one series,
  a small editorial palette (indigo, amber, teal, rose, slate) for several,
  lifted for dark mode. Tables are booktabs (three hairlines), first 6 rows,
  a fade at the far edge when wider than the column.
- **Motion** (first appearance only, none with reduced motion): fade + 4px
  rise; bars grow one after another; a line draws left to right, then its
  last value appears; table rows follow each other; the caption arrives last;
  the loading outline crossfades into the chart. The renderer re-creates the
  block while text streams after it — verified frame by frame that this does
  not flicker or replay the entrance.
- **highlight** — `{{show:<id> highlight="USA"}}`: the named categories in
  full colour, the rest tinted; checked against the result (unknown values
  ignored, at most 3, escaped). In the prompt's main example.
- **••• menu** reuses the result card's flows: Add to dashboard
  (`POST /api/artifacts/report/:id/add-visualization`), Save as query
  (`EntityCreateModal`), Download CSV (client), Excel
  (`/api/steps/:id/export?format=xlsx`), image (SVG → 2x PNG), Copy data
  (TSV), View code (`useCanViewCode`). Shared links: downloads only.
- **Channels** (Teams/WhatsApp/Slack/Google Chat): a figure becomes a
  footnote of the paragraph it followed (`footnote_citations`), so the reader
  still learns which result backs the text; plain-text surfaces strip it.

Live (Haiku 5.5, `tools/agent/figures_ui_flow.mjs`): every menu action
verified — CSV 371 B, Excel 5.9 KB, PNG 54 KB, 25 lines copied, save dialog
opened, "Added to dashboard", panel opened; the Hebrew answer used
`highlight="USA,Canada,France"`. Results: `media/pr/claude-figures-in-answers/live-results.json`.

**Model adoption is not guaranteed.** Across the day, Haiku 5.5 sometimes
answered with neither markers nor citations even though the rules were in the
prompt (verified on the live path: citations enabled on every planner call).
One streak of 7 runs had none; the following runs drew figures in 3 of 4.
`highlight` was used once in 4 runs after it moved into the main example.
Worth an eval before relying on it.
