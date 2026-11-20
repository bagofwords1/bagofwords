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
