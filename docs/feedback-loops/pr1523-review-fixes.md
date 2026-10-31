# Chat widget review fixes — PR #1523

Reviewed baseline: `48e712425`. The ten inline review threads and four earlier
findings describe failures in chat recovery, history rendering, publishing and
unbounded reads. This loop reproduces them with the real iframe/SDK in Chromium
and the real API/services/database, controlling external host/provider boundaries.

## Validated causes and changes

| Area | Cause | Change |
|---|---|---|
| Stop | `serverStop` swallowed failures and retained the optimistic stopped status | Surface the error, release the hold, restore a retryable answer even when history also fails; generation checks protect another conversation |
| Polling | Failed history/list polls never scheduled their next attempt | Retry within the existing interval and attempt bound |
| Refreshed results | `_chatSameResult` compared shape rather than content | Compare bounded JSON contents, including redacted rows, headings and chart hints; retain genuinely unchanged result objects |
| Duplicate send | Only a local stream blocked `send`; loaded history could also lag React's render | Block while the server answer runs, including a send queued during the history load |
| Hidden tables | Replacing `ChatMessages` lost Markdown and earlier steps | `showResults={false}` on both message components; document `text` versus `steps` |
| Publish | A missing input skipped validation of all other parameters | Validate identity and supplied values before storing a waiting frame |
| Running history | A result still being persisted was classified as permanently missing | Emit the incomplete-results notice only after the answer ends |
| Export | Existence checks decrypted every grid; resolving the newest viewer result fetched older rows too | Stream newest-first with early exit; limit the final viewer-result lookup to one row |
| Publish status | Each poll fetched every full step grid | Use the existing stored summary; fall back only for legacy rows without a summary |
| Dependency waits | Every downstream step rebuilt the release catalog and resolved the same dependencies | Keep lookup and wait metadata within one viewer run, replacing it after each stored result |
| Preview session | An automatic check invalidated manual preview handles silently | Return `preview_session_closed` and `next_browser_action: browser_navigate` |
| Host context | A shared top-level prompt field required route-specific clearing | Use `prompt.platform_context.host_context`; replace other client context with server-derived values |
| Test clocks / noise aliases | Serialization tests depended on elapsed wall time; unused aliases exposed obsolete internals | Assert UTC and round-trip identity; remove aliases and their private-symbol test |

Code entry points: `frontend/public/libs/artifact-globals.js:2294` (result
comparison), `:2540` (queued send), `:2676` (Stop),
`backend/app/services/dashboard_publish.py:123` (status reads), `:603` (validation),
`backend/app/routes/dashboard.py:420` (export probe),
`backend/app/services/dashboard_runs.py:247` (dependency waits),
`backend/app/services/artifact_conversations.py:383` (history),
`backend/app/ai/tools/artifact_verification.py:656` (preview cleanup).

## Setup

Use Python 3.13 and the repository's development dependencies:

```bash
cd backend
uv sync --frozen --extra dev
export BOW_DATABASE_URL=sqlite:///db/app.db
export TESTING=true
mkdir -p db
```

Frontend dependencies and vendored libraries must be present. Set
`PW_CHROMIUM_PATH` to the installed Chromium executable when Playwright's
default browser is unavailable. No live model or customer credentials are used.

## Browser loop

From the repository root:

```bash
node frontend/tests/unit/artifactConversationRecovery.mjs
node docs/feedback-loops/pr1523-review-fixes/capture.mjs
```

Observed before the fixes: failed Stop appeared stopped without an error; a failed
poll made no later request; same-shape refreshed results retained old values;
direct send posted while a reopened answer ran; both components ignored the
hide-results option. The initial regression run had eight failing scenarios.

After the fixes: **12/12 scenarios pass**, including late `started`, navigation
during Stop failure, simultaneous Stop/history failure, list/history retries,
result-content replacement, unchanged-object retention, direct and queued sends,
and both hide-results components. A newly added queued-send scenario exposed
the React batching race in the first implementation; synchronizing loaded history
before resolving the load fixed it.

Evidence under `media/pr/chat-widget-review/`: same-size before/after English and
Hebrew screenshots, failed Stop and retry screenshots, plus `flow.gif` and raw
browser recordings. The capture script serves the baseline globals for “before”.
These are real artifact iframe components with a controlled host transport;
they are not screenshots of a live ERP integration. The bubble request-envelope
change itself has no visual effect.

## API and performance loop

From `backend`:

```bash
uv run python ../docs/feedback-loops/pr1523-review-fixes/reproduce-performance.py
# Expected against baseline definitions: 3 failed, 1 passed.

uv run pytest tests/e2e/dashboards/test_context_status_reads.py \
  tests/e2e/dashboards/test_context_dependency_reads.py \
  tests/e2e/dashboards/test_viewer_export_read_budget.py --db=sqlite -q
# Fixed implementation: 4 passed.

uv run pytest tests/e2e/dashboards/test_context_publish.py \
  tests/e2e/dashboards/test_artifact_conversations.py \
  tests/e2e/test_artifact_automatic_check.py \
  -k 'validates_other_parameters or running_answers_do_not or functional_edit_is_checked' \
  --db=sqlite -q
# Before: 5 failed. After: 5 passed.
```

The performance reproducer loads baseline function definitions in its own
process; it does not modify checkout files. Measurements count actual database
reads and real Fernet decryptions, without machine-dependent timing assertions.

| Measurement | Before | After |
|---|---:|---:|
| Full-grid queries per status poll, summaries present | 1 query fetching every default step | 0 |
| Legacy waiting step without a summary | Correct label | Correct label, only that row fetched |
| Catalog reads for eight waiting steps with a shared dependency | 9 | At most 3, for member and admin |
| Distinct encrypted cached grids decrypted to export the newest of twelve runs | 12 | 1 |

The final resolver's missing SQL limit was found while broadening the export
regression beyond the original probe; its regression failed before that limit
and passed afterward. Existing export tests also passed (13 tests with the new
budget regression). Existing loader-chain tests cover waiting propagation,
supplying input, and subsequently loading freshly executed rows.

## Broader verification and limits

- 199 SQLite API/integration tests passed across context publishing, chat,
  artifact conversations, exports, viewer runs, dependency loading, host apps,
  automatic checks and expiry serialization. The four additional performance
  tests also passed.
- 348 focused backend unit tests passed. The classifier file was also run
  independently: a combined run with it after DB-heavy suites exposed a logger
  capture/order failure; its standalone and reordered combined runs passed. Serialization/deprecation
  and subprocess teardown warnings remain in the existing test harness.
- 12 targeted PostgreSQL regressions passed (110.14 seconds), including all
  performance cases, invalid-parameter publication, running-history notices,
  preview feedback and host-context boundaries. Docker's PostgreSQL 15 test
  container could not initialize because its disk was full; these checks used
  an isolated temporary database on local PostgreSQL 14.15 instead. The temporary
  database and failed container were removed afterward.
- 15 frontend Chromium/Node files passed; affected controller suites were rerun
  after the queued-send correction. Python fatal-error lint, Python parsing,
  JavaScript parsing and `git diff --check` passed.
- The SDK reference remains within its enforced budget: 10,402 / 10,500
  characters. The globals URL revision is `chat-ui-2`.
- No schema migration. No live model, ERP, IdP/OBO, or air-gapped deployment was
  exercised for these review fixes. Those integrations are not changed here.
