# Feedback Loop — "/diagnosis: we should be able to filter by user/agent/day (exact or custom range)" + free-text search

The monitoring diagnosis page (`/monitoring/diagnosis`) could only narrow agent
runs by coarse period presets (All time / 30d / 90d) and by agent (data source).
The request: filter the agent-execution table by **user**, **agent**, **time**
(exact day or custom range), and **free-text prompt search**.

## Root cause (validated)

Not a bug — missing capability. Before the change:

- `MetricsQueryParams` (`backend/app/schemas/console_schema.py:7`) carried only
  `start_date` / `end_date` / `data_source_ids`; there was no user dimension
  anywhere in the console API.
- The diagnosis queries anchor on `AgentExecution`, which already has a
  `user_id` column (`backend/app/models/agent_execution.py`), so the filter is a
  plain `WHERE` — no new joins.
- The backend already accepted arbitrary `start_date`/`end_date`
  (`ConsoleService._normalize_date_range` snaps them to day bounds) and the
  summaries endpoint already accepted `prompt_search`
  (`backend/app/routes/console.py`) — but the UI never exposed either:
  `DateRangePicker.vue` offered only three presets and no search box existed.

## Loop A — deterministic reproduction (no external services)

Agent executions are only produced by a live agent run (an LLM boundary), so
the e2e test seeds them directly via the `seed_agent_executions` fixture
(`backend/tests/fixtures/console_metrics.py`) — 5 executions across 2 users and
2 calendar days — then exercises the filters through the real HTTP routes:

```bash
cd backend
TESTING=true uv run pytest tests/e2e/test_console_metrics.py::test_diagnosis_filters_by_user_time_and_prompt -q
```

Observed FAIL on pre-change code (with `backend/app` stashed): the `user_ids`
query param is silently ignored, so the per-user assertion fails —

```
        assert summaries()["total_items"] == 5
>       assert owner_only["total_items"] == 3
E       assert 5 == 3
```

## The fix

Backend:
- `user_ids` (comma-separated) added to `MetricsQueryParams`
  (`backend/app/schemas/console_schema.py`) — flows into every console endpoint
  via `Depends()`.
- Applied as `AgentExecution.user_id.in_(...)` in
  `get_agent_execution_summaries`, `get_diagnosis_dashboard_metrics`, and
  `get_diagnosis_timeseries` (`backend/app/services/console_service.py`).
- New facet endpoint `GET /console/diagnosis/users` (same `manage_settings`
  gate) returning the distinct users that have agent executions in the org —
  feeds the filter dropdown.

Frontend (`frontend/pages/monitoring/diagnosis.vue`,
`frontend/components/console/DateRangePicker.vue`):
- `DateRangePicker` gains an opt-in `extended` mode (used by /diagnosis only):
  Last 7 days preset, **Exact day** (single date input), and **Custom range**
  (from/to date inputs), emitted via a new `rangeChange` event.
- User multi-select (searchable) fed by the facet endpoint; sends `user_ids`
  to the summaries, KPI-cards, and timeseries endpoints.
- Debounced free-text search box wired to the existing `prompt_search` param
  (narrows the table).
- Existing behaviors kept: agent (data source) selector, clicking an activity
  bar narrows the table to that day.

Re-run of Loop A after the fix:

```
1 passed, 285 warnings in 9.06s
```

Full file: `uv run pytest tests/e2e/test_console_metrics.py -q` → `13 passed`.

## Loop B — live confirmation (sandbox stack, stubbed LLM boundary)

```bash
tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py --demo
# seed users/reports/agent executions across days (no LLM needed):
#   scratchpad script inserting completions + agent_executions + tool_executions
#   for admin + 2 invited members over a 14-day spread
```

Before/after screenshots captured with `tools/agent/capture.mjs` against the
seeded stack live in `media/pr/diagnosis-filters/` (attached to the PR):
filtering by user narrows the table, KPI cards, and activity chart; exact-day
and custom-range narrow by time; the search box narrows by prompt text.

## What this proves / regression notes

- Filters compose (user × time × search × agent) and are applied server-side —
  pagination counts stay correct.
- The test asserts the general invariants (per-user counts, union of users,
  exact-day = start==end, range excludes out-of-range rows, facet lists exactly
  the users with executions) rather than one magic scenario.
- Pre-existing quirk (unchanged): with no explicit dates, the backend defaults
  to the last 30 days (`_normalize_date_range`), so the "All Time" preset
  actually shows 30 days. Custom range now gives a workaround; fixing the
  default is out of scope here.
