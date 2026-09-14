# Feedback loop — an artifact renders, but its interactions are unverified

This change lets the main agent exercise a saved data app through the existing browser tools, without a browser connector. It checks the actual parameter → viewer query → artifact runtime path and returns evidence alongside each action. It does not certify every metric or every possible interaction.

## Reproduced gaps and fixes

- The original `BrowserNavigateInput` requires a URL and has no artifact target. Existing browser operations address the top-level page, while generated apps run in an iframe. `browser_navigate(artifact_id=...)` now opens the exact saved version through the real `ArtifactFrame`.
- A successful HTTP query does not establish that the rendered app received its result. The preview broker correlates request IDs with the frame host's data revision and runtime acknowledgement. Incorrect applied parameters, missing requests, late/stale acknowledgements, and runtime errors remain distinct outcomes.
- The old compact snapshot filter removed unquoted generic text, including KPI values. It now removes only empty structural containers. An open dialog becomes the snapshot root so its controls survive the snapshot budget.
- Live QA caught an implementation regression: chaining `locator('option')` from an iframe `aria-ref` returned no options even though the resolved element was a native SELECT. Reading the resolved element's `options` collection fixes both label and value selection. The real-browser regression below failed twice before this fix and passes afterward.
- Live model testing exposed repeated malformed query IDs. Filter checks now accept `expect_query_update={"params": {...}}` and derive every affected query from the authorized manifest; an explicit query subset remains optional. Failure observations also retain the valid parameter/query manifest and a fresh snapshot; permission failures deliberately return neither prior data nor snapshots. Internal snapshots retain up to 8,000 characters inside the existing bounded context budget.
- The first generated ledger contained 1,000 of 2,240 lines, so client-side totals understated sales. Evidence now exposes returned rows, total rows, and truncation. The demo was repaired with a backend daily aggregate query; organization row limits were preserved.

Implementation entry points: `backend/app/services/artifact_preview_service.py`, `backend/app/ai/tools/implementations/_artifact_browser.py`, `backend/app/ai/tools/artifact_verification.py`, `frontend/components/dashboard/ArtifactFrame.vue`, and `frontend/composables/useBlockGrouping.ts`.

## Loop A — isolated regression tests

Use Python 3.12 and the repository's locked dependencies. The test fixtures create their own SQLite database. Do not point `TEST_DATABASE_URL` at a working application database.

```bash
cd backend
uv sync --frozen --extra dev
export BOW_DATABASE_URL=sqlite:///db/app.db
# In a provisioned cloud sandbox, use its existing browser cache:
# export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
TESTING=true uv run pytest tests/unit/test_artifact_browser_verification.py -q --disable-warnings
```

Coverage includes exclusive navigation targets, proportional hints, functional vs cosmetic edits, expected queries and applied parameters, pending/missing/failed updates, stale acknowledgements, partial-result evidence, blocked API writes, data visibility, browser session ownership, context replay, and real iframe select/detail operations. Only HTTP responses are substituted at the browser-contract test's transport boundary; selectors, snapshots, native controls, and dialogs run in Chromium. Those browser cases explicitly skip if Chromium is not provisioned.

Observed red for the iframe contract:

```text
ValueError: Select an exact option label or value from the snapshot
2 failed, 32 deselected
```

The same cases then passed. Full retained output: [red](internal-artifact-browser-verification/iframe-contract-red.txt), [focused tests](internal-artifact-browser-verification/unit-tests.txt), [surrounding regressions](internal-artifact-browser-verification/unit-regressions.txt), [frontend build](internal-artifact-browser-verification/frontend-build.txt).

Frontend grouping regression:

```bash
node frontend/tests/unit/useBlockGrouping.mjs
```

This checks first-call grouping, screenshot/repair membership, findings, prose visibility, planning gaps, stopping, steering boundaries, and ordinary tool grouping. All new locale keys are present across the ten catalogs; the existing key drift is not increased.

## Loop B — actual app and model

Used the existing frontend at `http://localhost:3000`, its existing backend, and normal `backend/db/app.db`, as requested. Temporary Playwright browsers were authorized. No replacement application stack was started. The existing backend's reload child stalled during code changes; only that child was recovered in place, leaving its supervisor, frontend, and other processes alone. Existing vendor assets were restored with `scripts/download-vendor-libs.sh` after a missing PDF library produced an HTML-as-JavaScript error.

Authentication remained in temporary local scripts/browser state. No login secret, JWT, or storage state is included in the repository. All showcased query data is the synthetic Music Store dataset.

### Existing app compatibility

Report `8426b9fe-6b84-49ae-97ab-0cd17ea0ee0e`, Solstice Sales v4, was exercised without editing its source. Its custom country popover, Canada selection, and reset used real viewer queries. Canada displayed **$303.96**; reset restored **$2,328.60**. Each successful filter action returned its snapshot plus `data_received` evidence without a follow-up inspect call.

### New complex app

Report `fbac6f15-4a32-417f-8550-67e6f4e23b78`, **Record Room**, was generated by **GPT-5.6 Luna** through the actual main agent. The initial creation automatically selected browser verification from its hint. Country, genre, minimum revenue, date range, and reset produced real backend updates. The repair added a daily aggregate query sharing the same four parameters with the detail ledger.

The final Luna pass completed **8 browser calls with no findings**: navigate, four backend filter/reset actions, open detail, close detail, and one screenshot. Each of the four backend actions observed both queries and matching runtime acknowledgements. No follow-up inspect/snapshot calls were used. USA revenue was **$523.06**; USA + Rock was **$155.43**; the empty case showed **$0.00**; reset restored **$2,328.60**. Machine-readable results are in [live-results.json](internal-artifact-browser-verification/live-results.json).

The final app uses full-population aggregates for revenue, orders, units, and trend. Its detail table explicitly identifies the returned subset. Verification checks both query results, empty state, reset, and the transaction detail dialog. Earlier unsuccessful attempts are retained in the conversation; findings are not erased or relabeled as passes.

### New static dashboard

Report `ae0f4368-8d40-48da-97e9-0a388af0f6d8`, **Music Store Snapshot**, was generated by Luna from one backend aggregate query. It has no filters, forms, tabs, or detail interactions. Its hint is not recommended and the main agent made **zero browser verification calls**.

Haiku was tried first but its configured provider account lacked credit. All completed model scenarios used Luna; this is not a Haiku result.

## UI evidence

Before/after screenshots, expanded evidence, Hebrew dark mode, and two recordings are under `media/pr/internal-artifact-verification/`. The group uses the existing spinner and shimmer, changes titles as the work advances, expands to show evidence, and preserves prose outside collapsed tools. Browser results show the exact version, applied parameters, returned row counts, duration, and warnings.

The before/after grouping comparison uses the same saved conversation at the same viewport: the before capture uses the base revision's grouping module; the after uses this change. It demonstrates the transcript presentation change, not a claim that the base revision could execute internal verification.

## Deployment and limits

- Organization AI Settings → **Verify data apps** is off by default. Enabling it requires **Allow LLM to see data** and an accessible saved page artifact in the current report before browser verification is available. It does not disable normal artifact viewing.
- `BOW_ARTIFACT_PREVIEW_URL` selects the reachable frontend origin; `BOW_ARTIFACT_BACKEND_URL` selects the broker's backend origin. Defaults use the configured frontend and `http://127.0.0.1:8000` backend.
- Chromium and vendor assets must already be installed in the deployment image. Verification performs no browser/runtime downloads. A fresh air-gapped container and PostgreSQL were **not** exercised in this local pass.
- The broker permits only the artifact's declared viewer-query path plus scoped reads/assets. Normal viewer identity and query guards still apply. Arbitrary writes and MCP calls are blocked; this is not a new write-capable bridge or a database transaction-level read-only guarantee.
- Internal preview sessions are isolated by organization, user, report, and execution, with bounded calls, query runs, and lifetime. Access is rechecked on subsequent operations. Data-visibility denial returns no prior evidence.
- A runtime acknowledgement proves delivery to the runtime, not semantic correctness. The model must compare visible results and appropriate expectations. Heuristic verification hints and bounded checks cannot guarantee every generated app is correct.
- Legacy/shared-viewer behavior is covered by surrounding tests; the live demonstrations used the authorized report owner. Other roles and withheld-snapshot deployments require their normal viewer access and may return a scoped unavailable result.

## Observed completion record (2026-09-14)

| Check | Observed result |
| --- | --- |
| Focused verifier + browser + runtime compatibility suite | 79 passed, 0 skipped; [output](internal-artifact-browser-verification/final-focused-tests.txt) |
| Surrounding artifact/browser/context regressions | 190 passed, 2 failed on both base and this change, 1 skipped |
| Production frontend build | Passed in 255.17 seconds; no replacement server started |
| Four-parameter combination | Country + genre + 2021 date range + minimum line revenue applied to both queries; reset restored $2,328.60; [evidence](internal-artifact-browser-verification/date-combination-results.json) |
| Grouping lifecycle | All assertions passed; [output](internal-artifact-browser-verification/frontend-grouping.txt) |
| Locale sync | No increased drift in any of 10 catalogs; [counts](internal-artifact-browser-verification/locale-sync.json) |
| Actual Luna complex verification | 8 calls, 4 two-query updates, details open/close, screenshot, no findings |
| Actual Luna static generation | Not recommended; 0 browser calls |
| Normal viewer recording | USA + Rock $155.43 → empty $0.00 → reset $2,328.60 → detail → close |
| UI evidence | Before/after, expanded evidence, Hebrew dark mode, data app, filters, empty state, dialog; recordings reviewed |

### Baseline test attribution

Both `test_read_query_loads_only_requested_query_graph[query]` and `[visualization]` fail unchanged on isolated base checkout **196146240**: [base output](internal-artifact-browser-verification/base-query-loading.txt). They assert that no SQL statement mentions `FROM reports`, while `read_query.py:267` now performs the existing scalar source-access check in `bow_source_access.py:70`. This does not hydrate the Report ORM graph; those assertions pass. The scalar authorization read was introduced by **9f910b5615**, before this PR; the test's latest rename was **d86977bdb**. Neither implementation nor test was changed here. Classification: **stale-test**. The authorization check was preserved.

The skipped surrounding test is the existing live-Babel gate, which defers when its browser/libs probe is unavailable in the test environment. It is not counted as a pass. The new iframe contract tests ran against actual local Chromium and passed.

The cache-version mismatch and native-select regression introduced during development were fixed and covered by the final green focused suite. The library's verification revision changes its cache URL while keeping runtime generation 11, preserving legacy-edit semantics.

The title-change recording is accelerated **4×**; the filter/detail recording is real time. Localhost links require the user's existing database and are supplementary examples, not dependencies of the isolated tests.

The connector lifecycle regression also failed before the fix ([red output](internal-artifact-browser-verification/connector-lifecycle-red.txt)) and passed afterward. Internal previews close at execution end; ordinary connector browsers retain their authorized user/report session across turns. Another user still cannot reuse it. The final 79-test pass includes both contracts and the existing browser tests.
