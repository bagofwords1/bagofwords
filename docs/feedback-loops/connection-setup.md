# Feedback loop — unified connection setup and discovery progress

The connection form remains visible beside discovery status, activity, and errors.
Connect tests and saves the connection in one action. Failed discovery unlocks the
same form; Retry updates that connection and reindexes it. Done closes a completed
run. The form uses consistent spacing, visible settings without an advanced toggle,
and a stacked layout on small screens. Primary buttons match the existing blue
Save and Continue control.

## Root cause and scope

- `backend/app/data_sources/clients/progress.py:57` resets `done` at every phase.
  The old UI reused one unqualified percentage bar for all phases. Consequently,
  90% of one phase followed by 5% of another looked like a restart. Percentages
  are now explicitly for the current stage, with previous stages retained above
  the detailed logs. A changed phase creates a new bar instead of animating the
  previous bar backward. This does not invent a global percentage or duration.
- `backend/app/services/connection_indexing_service.py` used to log only the
  phase visible at a database flush. Short intermediate phases disappeared.
  The callback now queues phase changes and bounded activity milestones;
  flushes persist them with the progress snapshot. Both errors and cancellation
  flush the pending activity before finalization. Failed flushes retain events
  for the next attempt. The 200-event cap remains.
- PostgreSQL's `get_schemas` previously accepted no progress callback. It now
  reports column reads/processing, materialized views, relationships, and the
  basic-metadata fallback without adding database queries.
- Modal polling now schedules the next request after completion and ignores
  responses from a closed/replaced polling session. Identical GETs already had
  transport-level deduplication; overlapping requests were not established as
  the cause of the reported reset.

This changes the database/data-source form path. The specialized MCP, custom API,
browser, and integration forms retain their own save flows. Existing edit and
onboarding callers of ConnectForm retain their separate Test/Save actions.

## Loop A — backend regression checks

```sh
cd backend
TESTING=true .venv/bin/python -m pytest \
  tests/e2e/test_connection_indexing.py \
  tests/unit/test_postgresql_progress.py \
  --db=sqlite -q --tb=short --disable-warnings
```

The PostgreSQL unit cases compare discovered catalogs and SQL call counts with
and without reporting, across empty/single/multiple-table catalogs and enriched/
fallback queries. They also verify that cancellation propagates without retrying
metadata discovery.

The API test `test_short_discovery_stages_remain_in_activity_log` emits several
phases before yielding the runner loop. It verifies history survives coalescing
on success and failure. The remaining indexing API tests cover create, rejection,
retry, idempotency, cancellation, and the inlined data-source payload.

Observed validation:

- Original PostgreSQL class loaded from Git into an isolated test process:
  **8 failures**, `get_schemas() got an unexpected keyword argument 'progress_callback'`.
- Updated PostgreSQL class: **8 passed**.
- Original indexing runner loaded from Git in a pytest collection hook:
  the short-stage test **failed**; returned phases were
  `[None, 'columns', 'tables', 'tables']`, missing `catalog`.
- Updated runner: all **7 indexing API tests passed** before adding the failure
  variant; both success/failure variants then **passed**. The combined suite
  passed **15 tests** before adding that second variant (**16 distinct passing
  backend cases** across the final runs).

These checks use isolated SQLite application databases and a seeded local source,
not a customer database. PostgreSQL metadata I/O is stubbed in the unit tests.

## Loop B — browser flow and visual evidence

Start the frontend, then run the saved browser harness:

```sh
cd frontend
npm run dev -- --port 3100
# In another shell, from frontend:
PLAYWRIGHT_BASE_URL=http://localhost:3100 node tests/data_sources/connection-setup-flow.mjs
```

The harness temporarily creates an unauthenticated preview route, uses the real
Vue components and actual backend connector field schemas, with synthetic API
responses for connection operations, and removes that route afterward.
It refuses to overwrite an existing preview file. It verifies:

- Credentials failure renders in the status pane and preserves editable values.
- Connect performs the test and creates exactly one connection.
- Active discovery disables the submitted form.
- A 90% → 5% phase transition identifies the new stage and retains history.
- Discovery failure unlocks editing; Retry updates/reindexes the original ID.
- Done is shown on completion and closes the modal. Closing a saved connection
  with X during discovery also refreshes the caller exactly once.
- Mobile layout, Hebrew RTL, dark mode, and all ten locale renderings.

The flow passed without browser page errors. Screenshots and the short flow GIF
live in `media/pr/connection-setup/` (before, after, progress, connection error,
discovery error, completed, mobile, Hebrew, and dark mode).

The standard `playwright.i18n.config.ts` suite was attempted but its public routes
remained on the app startup/loading screen in this local environment. It was
stopped after repeated startup failures. This is not reported as a passing check.
The targeted modal harness renders all ten locales; catalog missing/extra-key
counts remain unchanged from baseline (including existing catalog drift).

## Build verification

The production Nuxt build completed successfully (client, server, and Nitro
packaging). The final close-notification guard also passed Vue script/template
compilation and the focused browser flow.

## Follow-up corrections and Test again

The generic select renderer previously read only `enum`, while PostgreSQL supplies
`ui:options`: the before capture reproduced **0 choices instead of 6**. The shared
renderer now accepts `enum`, `ui:options`, and `options`, preserving numeric values.
Empty credential wrappers and stacked spacing rules caused uneven field gaps.
Explicit section/grid spacing fixes this, and the advanced heuristic is removed.

Run `node tests/data_sources/connection-form-regressions.mjs` from frontend with
the same local server. This verifies actual PostgreSQL and Teradata choices,
numeric SQL Server choices, test invalidation in edit/onboarding, consistent
Power BI spacing, delegated-auth warnings, and primary button computed styling
matching legacy Save and Continue. Evidence: `media/pr/connection-setup-corrections/`.

Test again appears beside Done after discovery completes. It calls only
`POST /connections/{id}/test`, retains discovery results, and displays the newest
test outcome separately. The existing endpoint updates connection health, including
its active flag; it does not resave configuration or start discovery. Failed tests
leave Done available. A session guard ignores responses after reopening the modal.
Error text is unchanged, as requested.

The full browser flow now passes, including failed and successful repeated tests
with unchanged create/update/reindex counts, test-before-save rejection, retry on
the original connection, and exactly one caller notification from Done or X.
It also passes ten-locale rendering, dark mode, Hebrew RTL and mobile checks.
New evidence: `retest-failed.png`, `retest-passed.png`, and the recorded flow under
`media/pr/connection-setup/`. These are deterministic UI/API-boundary tests, not
live third-party connection checks.

## Edit mode follow-up

`ConnectionDetailModal.vue` and `AgentConnectionsModal.vue` now share
`EditConnectionModal.vue`: the same two-pane dimensions, embedded form, spacing,
and blue primary action as Add Connection. Existing specialized MCP/API/integration
editors retain their connector-specific forms.

ConnectionDetail previously cleared `testResult` when closing to open Edit.
The transition now snapshots that result and passes it to Edit. The agent-list
entry also supplies its latest local test result. The right pane fetches the
latest indexing run (and polls active runs) so discovery errors/logs appear too.
It does not silently run a new connection test on opening. A new test replaces
the displayed test outcome and timestamp, while retaining discovery history.

The backend currently persists test status/time, not the full original test
message. Consequently, exact test text is carried from the current UI session;
after a full page reload, only cached status/time and persisted discovery errors
are available. This change does not add a database migration or pretend an old
message was stored.

Reproduction/verification: from frontend, run
`node tests/data_sources/connection-edit-flow.mjs` with the local server at 3100.
Passed: existing test and discovery errors visible on open; no implicit test;
Test again performs no save; failed validation blocks Save; successful Save
updates once and retains locked credentials; Hebrew RTL/mobile; no page errors.
Evidence: `media/pr/connection-edit/`. The before image is the user's supplied
legacy Edit screenshot; after images/video use the real components and actual
PostgreSQL field definitions with synthetic API outcomes.
