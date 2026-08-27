# Artifact iteration speed, regressions, and dashboard filtering — diagnosis & plan

Status: diagnosis + proposed plan (no code changes yet)
Trigger: customer complaints — iterating on an artifact is time-consuming, edits
regress other areas of the page, and data isn't filtered the way users expect
from a Tableau-style dashboard (filters, parameterized queries).

## Summary

Three distinct problems hide behind "iteration is painful":

1. **Latency** — every edit pays one large-context LLM call plus up to five
   render-repair rounds, each launching headless Chromium with an 8s render
   wait, and the frontend then tears down and remounts the whole iframe.
2. **Regressions** — the edit path *is* a surgical diff, but three design
   choices undermine it: the planner routes medium changes to a full
   `create_artifact` rewrite, generated code addresses data **positionally**
   (`viz[0]`, `viz[1]`…) so any viz add/remove forces a global hand-renumbering,
   and the edit prompt deliberately drops conversation history / accumulated
   spec, so earlier design decisions aren't defended.
3. **Filtering** — a complete server-side parameterized-query system already
   exists end-to-end, but it is opt-in per query at `create_data` time only and
   the planner is told not to use it by default. Most user-visible "filters"
   are client-side row filters over a pre-fetched snapshot, which go dead
   whenever a viz doesn't happen to project the filter column.

Each has targeted fixes; none requires a rewrite of the artifact system.

---

## 1. Why iteration is slow

Cost chain for a single "make the bars blue" request:

1. Planner turn (often preceded by a `read_artifact` call, since the planner's
   `<current_artifact>` context contains metadata but never code —
   `app/ai/agents/planner/prompt_builder.py:476-523`).
2. `edit_artifact` builds a prompt containing the **full artifact code plus all
   viz profiles** (`edit_artifact.py:351`), then one streaming LLM call
   (`edit_artifact.py:1074`), plus a possible diff-repair retry call
   (`edit_artifact.py:650`).
3. **Render validation before persisting**: `_validate_and_repair_stream`
   (`create_artifact.py:653`) launches Playwright/Chromium
   (`_take_preview_screenshot`, `create_artifact.py:97`), waits for
   `networkidle` and up to 8s for `__ARTIFACT_RENDER_COMPLETE__`.
4. Up to `MAX_RENDER_REPAIR_ATTEMPTS = 5` repair rounds
   (`create_artifact.py:377`), each = one more LLM call **and one more browser
   launch**, under a 210s deadline (`edit_artifact.py:713`).
5. Background thumbnail render (another browser session).
6. Frontend: `artifact:created` → `switchToArtifact`
   (`ArtifactFrame.vue:1623`) → refetch artifact + **all** viz data, rebuild
   `srcdoc`, full iframe teardown/remount. All client-side filter state,
   scroll position, and local UI state are lost on every iteration.

Worst case: one edit ≈ 2–7 LLM calls + up to 6 Chromium launches.

### Fixes (ordered by leverage / risk)

- **S1. Pool the validation browser.** Keep one warm Playwright
  browser/context per worker instead of launching per validation round; reuse
  it across the repair loop. This is the single biggest deterministic win —
  browser cold-start × up to 6 per edit.
- **S2. Cut the render wait.** `__ARTIFACT_RENDER_COMPLETE__` already signals
  readiness; `wait_until="networkidle"` on top of it mostly adds fixed latency.
  Wait on the signal with a short fallback, not both.
- **S3. Skip full validation for non-structural edits.** Classify the applied
  diff: if it touches only literals/classNames/text (no JSX structure, no
  `viz[...]` references, no hooks), a syntax/lint check suffices — persist and
  let the background thumbnail catch visual drift. Structural edits keep the
  full render gate.
- **S4. Fall back to rewrite instead of failing.** Today, if diffs don't apply
  after one retry the tool **fails** (`edit_artifact.py:1174`, `:1204`) and the
  user pays a whole new planner round-trip. `edit_doc` already has the right
  contract (surgical diff with full-rewrite fallback — see
  `docs/design/doc-artifacts.md`); port it to `edit_artifact`.
- **S5. Don't remount the iframe on edit.** The live-update channel already
  exists (`postMessage ARTIFACT_DATA`, `ArtifactFrame.vue:1834`) and is used
  for param runs precisely so control state survives. Extend it with an
  `ARTIFACT_CODE` message that swaps the compiled component in place when only
  code changed; fall back to remount when viz sets or libs changed. At minimum,
  snapshot `useFilters`/`useParams` state before remount and re-seed after.
- **S6. Include a compact code outline in `<current_artifact>`** (the outline
  `read_artifact` already computes — `read_artifact.py:68`) so the planner can
  route create-vs-edit and target sections without a separate `read_artifact`
  round-trip in the common case.

## 2. Why edits regress other areas

The edit itself is an aider-style SEARCH/REPLACE diff
(`edit_artifact.py:204`) — good. Three things around it cause the regressions:

- **R1. The >30% heuristic.** The planner is told to use `create_artifact` for
  "a change too large for surgical diffs (~>30% of code)"
  (`prompt_builder.py:186`). A medium restyle therefore regenerates **every**
  panel from scratch — the classic "I asked to change the header and my charts
  changed" report. Fix: raise the bar to "rebuild only on explicit redesign
  request or viz-set overhaul", teach `edit_artifact` to take many hunks in
  one call, and when a rebuild is genuinely needed, pass the prior code to
  `create_artifact` as a reference with an explicit "preserve everything not
  named" instruction (today the rebuild prompt doesn't include it).
- **R2. Positional viz indexing.** Generated code reads `viz[0]`, `viz[1]`…,
  ordered by `content.visualization_ids` (`ArtifactFrame.vue:1955-1965`).
  Removing or adding a viz re-indexes the array, and the model is instructed to
  hand-renumber every surviving reference (`edit_artifact.py:415-421`; new-viz
  auto-merge shifts indices again at `:840-860`). One missed index silently
  points a chart at the wrong dataset — the worst regression class because it
  renders *plausibly wrong data*. Fix: make data access **id-keyed**
  (`vizById("<uuid>")` or a stable alias map injected by the sandbox
  runtime), keep `viz[N]` working for old artifacts via a compatibility shim in
  `artifact-globals.js`, and update the create/edit prompts. Add/remove then
  becomes local: no renumbering, no blast radius.
- **R3. The edit prompt has no memory.** Page-mode edits deliberately omit
  conversation history and the accumulated spec to control token growth
  (`edit_artifact.py:371-378`), so a later edit can undo an earlier explicit
  decision ("keep the KPI row on top"). Fix: maintain a **pinned-decisions
  digest** — a bounded (~1–2k token) list of durable design constraints
  appended on each edit (the accumulated-spec mechanism at
  `edit_artifact.py:1400` already persists per version; distill it instead of
  dropping it) — and include only that digest in the edit prompt.

Cheap guardrail to add alongside: after applying diffs, statically verify that
the set of viz references in the new code equals the expected
`visualization_ids` set, and fail the specific hunk (not the whole edit) when a
reference disappears unexpectedly — a deterministic version of the Step C
"superset" prompt rule (`prompt_builder.py:226-233`).

## 3. Filtering: promote parameters to first-class dashboard citizens

What exists today:

- **Server-side query parameters** — complete and audited:
  `ParamSpec` (`app/schemas/param_schema.py:51`) with types, defaults,
  identity bindings, and `options_source` (another query's column as the
  filter space); safe rendering in
  `app/ai/code_execution/query_params.py`; viewer-mode execution with a
  per-user, per-fingerprint cache (`query_service.run_query_viewer`,
  `services/query_service.py:606`); `useParams()` in the sandbox and URL
  sharing in the host (`ArtifactFrame.vue:1166-1195`).
- **Client-side filters** — `useFilters()`/`filterRows` over already-loaded
  rows (`public/libs/artifact-globals.js:158-235`).

The gap is not capability, it's **defaults and lifecycle**:

- Parameters can only be declared at `create_data` time; there is no way to add
  one to an existing query without regenerating it.
- The planner is explicitly discouraged: "Do NOT speculatively parameterize
  every literal — no params is the common case"
  (`app/ai/tools/schemas/create_data.py:95`).
- There is no cross-query "dashboard parameter" object, so Tableau-style
  global filters are emulated by client-side `filterRows` — which silently does
  nothing for any viz that doesn't project the filter column (hence the
  FILTER FEASIBILITY AUDIT and Dashboard Contract preflight in the prompts,
  which paper over the mismatch rather than remove it).

### Plan

- **F1. Flip the default for dashboards.** When the terminal deliverable is a
  page-mode artifact classified as a "filter" Dashboard Contract
  (`prompt_builder.py:191-225`), the planner should parameterize the shared
  dimensions (date range + 1–3 low-cardinality dimensions) across **all** the
  dashboard's queries, using the existing filter-space pattern
  (`prompt_builder.py:145`). Client-side `useFilters` stays for cheap
  within-snapshot interactions, not as the primary filter mechanism.
- **F2. Report-level parameters.** A lightweight `report_parameters` concept
  (name, ParamSpec, bound query ids) so one control drives N queries. Much of
  the runtime already behaves this way (`paramSubsetForQuery`,
  `ArtifactFrame.vue:1045`, fans a shared param out to each bound query); the
  missing piece is the persistent object the agent and UI can address, instead
  of convention-by-matching-param-names.
- **F3. Retro-parameterization tool.** `add_parameter(query_id, param_spec,
  column)` — a small, targeted codegen step that rewrites an existing step's
  SQL/code to add `(:name IS NULL OR col = :name)` and re-runs it once. This
  removes the "regenerate the whole query to add a filter" tax, which is a
  large share of the iteration loops customers are complaining about (the
  Dashboard Contract preflight currently *mandates* `create_data` re-runs to
  add a filter dimension).
- **F4. Params changes shouldn't rebuild the page.** Adding/altering a filter
  control is a param-wiring edit; combined with S5 (no remount) and the
  existing viewer-mode cache, adjusting filters becomes a seconds-scale
  operation with no LLM in the hot path once wired.

## Sequencing

1. **Quick wins (latency):** S1, S2, S4, S6 — no product-behavior change,
   directly attack the slowest constants.
2. **Regression hardening:** R2 (id-keyed viz access + shim) first — it removes
   the worst failure class; then R1 routing changes and R3 pinned-decisions
   digest; S3 rides along once diffs are classified.
3. **Filtering:** F1 prompt/default changes (cheap, immediate customer-visible
   value), then F3 retro-parameterization, then F2 report-level parameters,
   with S5/F4 closing the no-remount loop.

Each phase is independently shippable and testable with the
`sandbox-feedback-loop` skill (edit-latency timing, regression checks on viz
reference sets, filter behavior across vizzes).
