# Feedback Loop — "Rerunning a report does not update content"

Reproduces and validates the reported bug: on an artifact-based report (the
current dashboard format), clicking **Refresh Data** / hitting
`POST /reports/{id}/rerun` returns 200 and a success toast, but the dashboard
keeps rendering stale — or, after the retention window, empty — data forever.
Observed live on a report whose KPIs all showed ₪0.00M: every query's default
step had `data = {}` and rerun changed nothing.

Three stacked causes were validated, plus a fourth found while proving the fix:

1. **Rerun never touches what artifact reports render.** Artifacts reference
   visualizations via `artifact.content.visualization_ids`; the dashboard
   fetches each query's **default step** (`routes/query.py` `get_default_step`).
   The old `rerun_report_steps` only walked dashboard-layout `visualization`
   blocks — a deprecated mechanism artifact reports never populate (their
   active layout has 0 blocks) — then fell back to rerunning each widget's
   **newest** step (`widget_service.run_widget_step` → `_get_last_step`),
   which is not the step the artifact reads whenever a later attempt exists.
2. **The retention purge can strip a shared dashboard's data.** The daily
   purge (`maintenance_service.purge_step_payloads_for_organization`) nulls
   `steps.data/data_model/view` after `step_retention_days`, excluding shared
   reports **only via the legacy fields** (`reports.status = 'published'`,
   `conversation_share_enabled`). The actual sharing source of truth —
   `artifact_visibility` / `conversation_visibility` — was never consulted, so
   any drift in the legacy sync silently wipes a shared dashboard. (The
   changelog promise "restore anytime by rerunning" was also broken for
   artifact reports by cause 1.)
3. **The endpoint is slow even as a no-op** (~5.6s observed live). The rerun
   path hydrated the full report graph — `db.get(Report)` and the fallback's
   bare `select(Report)` trigger the mapper-level `lazy="selectin"` cascade
   (every step version's `data` JSON, completions, artifact versions) — and
   each `rerun_step` re-hydrated the report and re-constructed data-source
   clients, then executed the (fully synchronous) step code **on the event
   loop**, stalling every other request.
4. **`last_run_at` was reported as `null` even after a successful rerun** —
   the field is persisted, but `get_report`'s hand-built `ReportSchema(...)`
   omitted `last_run_at` (and `last_activity_at`), so the API always returned
   `null` for both.

## Root cause (validated)

- `backend/app/services/report_service.py` (old lines 670-737):
  layout-blocks-driven rerun; artifact reports fall through to the legacy
  widget fallback, which reruns `_get_last_step` (newest by `created_at`,
  any status) instead of the default step, and swallows all failures while
  the route returns 200 + a full `ReportSchema`.
- `backend/app/services/step_service.py` (old lines 114-159): per-step report
  re-hydration + per-step `construct_clients` + synchronous `execute_code`
  call on the event loop.
- `backend/app/services/maintenance_service.py` purge SQL: no
  `artifact_visibility` / `conversation_visibility` guards.
- `backend/app/services/report_service.py` `get_report`: `ReportSchema(...)`
  constructor missing `last_run_at` / `last_activity_at`.

## Loop A — deterministic reproduction (real backend, no external services)

Step code is pure pandas (no data source), so the loop runs in a clean
sandbox. Queries/visualizations/artifacts are produced by the AI flow in
production, so the graph is seeded directly (same pattern as
`test_artifact_large_data_perf_repro.py`).

```bash
cd backend
uv run pytest tests/e2e/test_report_rerun_artifact.py tests/e2e/test_step_retention_purge.py -v
```

Observed on the pre-fix code:

```
FAILED test_rerun_refreshes_default_steps_behind_report_artifacts
    assert rows, f"default step of query {qid} was not refreshed by rerun"
    (default step data stays empty; the rerun response is a full ReportSchema
     with no run outcome)
FAILED test_rerun_reports_partial_failures_and_still_refreshes_the_rest
    KeyError: 'steps_total'  (failures silently swallowed)
FAILED test_rerun_cost_does_not_scale_with_stored_step_history
    assert 25 <= 8  (25 SELECTs against steps — full-graph hydration)
FAILED test_purge_never_touches_shared_reports_even_without_legacy_field_sync
    artifact_shared: step payload was purged from a shared report
```

(`last_run_at`: verified separately — the value was present in the DB row but
`GET /reports/{id}` returned `null`.)

## Loop B — live confirmation (what was observed on eu.bagofwords.com)

Report `6968e2cf-…` ("2025 Revenue Drop — Deep Dive Analysis"): active layout
with 0 blocks, artifact referencing 6 visualizations, all 7 default steps with
`data = {}` (draft report, steps from June 3, purged mid-June by the 14-day
retention), `POST /rerun` → 200 in 5.6s with no effect and `last_run_at`
still `null`. Re-executing one step directly surfaced the additional
environment problem: the underlying MSSQL connection fails
(`Login failed for user …`) — with the fix, that now shows up as
`steps_failed` in the rerun response instead of a silent success.

## The fix

1. **Artifact-driven rerun** (`report_service.rerun_report_steps`): resolve
   `visualization_ids` from the artifact the report renders — the
   `artifact_id` passed by the caller (the frontend sends the dashboard being
   viewed) or the report's **latest** non-deleted artifact (superseded
   versions live on as rows; unioning them would re-execute queries the
   dashboard no longer shows and count their failures). Each query's
   **default step** is rerun (same resolution as the read path,
   cross-referenced in `query_service.get_default_step_for_query`); queries
   that resolve to no runnable step are counted as failed rather than
   silently skipped, and two queries resolving to the same step run it once.
   Layout viz-blocks are no longer consulted; the legacy published-widgets
   fallback remains only for pre-artifact reports (and now passes
   `current_user` through so user-scoped connections resolve). The route
   accepts `?artifact_id=` and returns
   `{message, steps_total, steps_succeeded, steps_failed, last_run_at}`
   (`ReportRerunResultSchema`) instead of a full `ReportSchema`.
2. **Retention guard** (`maintenance_service`): the purge additionally skips
   reports with `artifact_visibility != 'none'` or
   `conversation_visibility != 'none'`, independent of the legacy sync.
3. **Perf**: the rerun loads the report once with `lazyload("*")` whitelists
   (data sources + files only), builds data-source clients and org settings
   once per run, resolves rerun targets with batched column-only selects,
   and `rerun_step` executes step code via the bounded code-exec thread pool
   (`StreamingCodeExecutor.execute_code_async` — same cap and contextvar
   propagation as the agent path) with result formatting off-loop too. The
   legacy fallback's report lookup no longer triggers the cascade. Thumbnail
   regeneration (a headless-browser launch) and subscriber notifications only
   fire when at least one step actually succeeded, and notifications only for
   scheduled runs (`notify_subscribers=True` from the scheduler) — an
   interactive Refresh no longer emails subscribers "your scheduled report
   ran".
4. **Serialization**: `get_report` now includes `last_run_at` and
   `last_activity_at`.
5. **UI honesty** (`frontend/components/dashboard/ArtifactFrame.vue`): the
   Refresh Data toast reflects the run outcome — green with a localized
   "{succeeded} of {total} queries refreshed" summary, orange "partially
   refreshed"/"nothing to refresh", red when everything failed (keys added to
   all ten locale catalogs). A failed rerun request no longer strands the
   dashboard behind the loading overlay, and the request scopes to the
   selected artifact via `?artifact_id=`.

Re-run Loop A on the fixed code:

```
7 passed
[rerun-perf] small=230ms (7 steps SELECTs), large=215ms (7 steps SELECTs)
```

— the same seeds that previously produced 25 steps SELECTs and empty default
steps now produce a bounded, history-independent rerun that repopulates
exactly the steps the dashboard renders, reports failures, and persists+serves
`last_run_at`.

## What this proves / regression notes

- The reproduction tests survive as regression guards:
  `tests/e2e/test_report_rerun_artifact.py` (artifact rerun contract, failure
  accounting, hydration/latency bounds) and
  `tests/e2e/test_step_retention_purge.py` (purge never touches shared
  reports in any mode; still purges stale private drafts; legacy flags still
  honored).
- The fix does not resurrect data whose upstream connection is broken — on the
  live report the MSSQL credentials must be repaired before the dashboard can
  repopulate; the rerun response now says so (`steps_failed`).
- Consciously accepted: transition-era reports that render dashboard-layout
  visualization blocks but have no artifacts fall through to the legacy
  widget fallback (layout blocks are deprecated per product direction); the
  honest run summary at least makes that visible as "nothing to refresh"
  instead of a silent green toast. Known follow-ups: consolidate the three
  artifact→query resolutions (`query_service.list_queries(artifact_id=...)`,
  `get_public_queries`, rerun) behind one helper, and express the purge's
  "report is shared" predicate once instead of four SQL conditions.
- UI evidence (full stack via `tools/agent/boot_stack.sh --dev`, artifact
  report seeded with two healthy queries + one whose code raises, all default
  step payloads nulled as after retention) in
  `media/pr/report-rerun-update-issue-tmepus/`:
  - `loaded-no-data.png` — the purged dashboard ("No data" everywhere).
  - `before-refresh.png` — pre-fix: after clicking Refresh Data, a green
    "Dashboard refreshed" toast over a still-empty dashboard.
  - `after-refresh.png` — post-fix: the two healthy queries render their
    rows and the toast honestly reports "Dashboard partially refreshed —
    Reran 2/3 report steps (1 failed)".
