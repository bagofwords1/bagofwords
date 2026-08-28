# Feedback loop — artifact iteration robustness & filtering (phases 1–2 + round 2 + F3 + phase-3 step 1)

Verifies the changes on `claude/artifact-iteration-filtering-2chyem`
(design: `docs/design/artifact-iteration-and-filtering.md`) against a full
local stack: backend (sqlite) + frontend + real LLM (Claude 4.5 Haiku) +
the bundled Chinook SQLite data source
(`backend/demo-datasources/chinook.sqlite`), driven through the real chat UI
with Playwright and verified at the DB / HTTP / rendered-iframe layers.

## Setup notes (sandbox quirks that cost time)

- `scripts/download-vendor-libs.sh` must run before artifacts render (React
  etc. are not vendored in the repo).
- Pin `BOW_ENCRYPTION_KEY` before first boot. It is auto-generated per
  process when unset; a backend restart otherwise mints a new key, which
  invalidates JWTs AND orphans encrypted provider/connection credentials
  (symptom: `KeyError: '<data source>:<connection>'` from every code run,
  because construct_clients silently skips connections it cannot decrypt).
- uvicorn `--reload` reliably wedges on "Waiting for background tasks to
  complete" in this sandbox; run without the reloader and restart manually
  after backend edits.

## Phase 1–2 verification (first round)

- Dashboard built by Haiku over Chinook: id-keyed `vizById("<uuid>")`
  bindings only (0 positional refs), genre + country declared as server-side
  input parameters on every dashboard query. Selecting Rock re-ran all 3
  queries at the source (3× `/api/queries/{id}/run` → 200); KPI moved
  $2,329 → $827, matching Rock's known $826.65.
- 5 edit iterations (restyle, add viz, rename, remove viz): after every
  version, code refs == payload id set; the dark-KPI restyle survived three
  later edits (pinned-decisions digest); removal left no orphan refs.
- Backward compat: a seeded positional `viz[N]` artifact rendered unchanged
  under the new runtime; first edit codemodded every ref to `vizById`
  (backend log confirms), applied the requested change, lost nothing; the
  stored legacy row was untouched.
- Doc create+edit (placeholders re-extracted) and slides create+edit
  (valid 3-slide PPTX, v2 after edit) unaffected.

## Round-2 verification (PR #1030 review findings)

- Coder prompt now offers a full-rewrite fallback instead of "output
  nothing" (edit_artifact system prompt).
- Gate hard-blocks on edits: unconverged `[viz refs]` errors reject the edit
  and keep the last good version (`edit_artifact` + `apply_artifact_edit`).
  Verified tool-level: an edit deleting a viz binding was rejected with a
  precise error and persisted nothing.
- Coverage tightened with the reviewer's counterexamples as unit tests:
  a chartless page (payload, zero refs) fails; `viz.slice(0,1)` /
  `.length` no longer count as coverage (`map`/`forEach` only).
- Codemod skips string literals and `//` comments (quoted "viz[0]" no
  longer becomes nested-quote breakage) — unit-tested.
- v3 planner DASHBOARDS section aligned: server-side params are the default
  filter mechanism; useFilters demoted to within-snapshot interactions.
- Digest: later entries supersede earlier on conflict; rebuild baseline
  discloses truncation beyond 30K chars.
- Unit: 17 tests in `tests/unit/test_artifact_refs.py`; 44 green across the
  artifact suites.

## F3 — add_parameter (retro-parameterization)

- Chat: "add a city filter to the existing Top 10 Artists query" →
  `add_parameter` succeeded in-place: query kept its id and viz, now
  declares `city_filter` (static options), new default step binds
  `:city_filter`, `edit_artifact` wired the control.
- UI: selecting Paris re-ran ONLY the bound query (1× `/run`) and the
  table switched to Paris-only revenue.
- Failure semantics verified live: an options_source pointing at the query
  itself is rejected by declaration validation; a failed execution rolls the
  declaration back (no declared-but-dead params — this rollback was added
  after the first attempt exposed the gap).

## Phase-3 step 1 — apply_artifact_edit (mechanical, planner-authored)

- Tool-level: exact-once find/replace applied atomically and persisted a new
  version with no LLM call; an edit that orphaned a payload viz was rejected
  by the hard gate with nothing persisted.
- Planner-level (Haiku): `read_artifact` → `apply_artifact_edit` changed the
  dashboard heading; new version rendered live ("Chinook Revenue Monitor
  2025") with filters intact.
- Payload note: `collect_artifact_payload` appends the report's other
  visualizations as stragglers (mirroring ArtifactFrame); the tool scopes
  gating/validation to the artifact's own merged id set.

## Known limitation recorded

- `apply_artifact_edit` falls back to persisting unvalidated when Playwright
  is unavailable in-process (same contract as create_artifact's missing-
  Playwright fallback). In-server, render validation runs; a standalone
  harness without a browser can persist a JSX-invalid edit. Acceptable for
  the same reason the create-side fallback is; revisit if a headless-less
  deploy target appears.
