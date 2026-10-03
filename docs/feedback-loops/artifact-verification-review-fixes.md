# Feedback Loop — stale failures and browser-session regressions

Review of PR #1133 identified stale snapshot errors, connector capacity and
session-recovery regressions, misleading access failures, pending checks counted
as findings, and cumulative query acknowledgements. This follow-up addresses
those contracts without changing the verifier's main-loop design.

## Root cause and correction

- `backend/app/services/artifact_preview_service.py:257`: bare snapshots used
  cursor zero while evidence delivery used its last delivered cursor. A delivered
  error therefore produced `failed` alongside an empty error list. Both now use
  the same floor; explicit action/history requests retain their history.
- `backend/app/ai/tools/implementations/_browser_common.py:328`: unknown and
  unauthorized IDs were indistinguishable to navigation. Strict lookup rejects
  live foreign sessions while expired IDs can open a fresh authorized page.
- `_browser_common.py:376`: the process-wide capacity cap stopped reclaiming
  idle connector sessions. Connector sessions now register active executions;
  execution cleanup releases those claims without closing the page. Capacity
  reclaims only inactive connectors, and neither capacity nor idle eviction
  interrupts active connectors or previews. Fully occupied active capacity
  still returns an honest, general browser-busy error.
- `artifact_preview_service.py:99`: every non-200 response became a permission
  failure. HTTP 401/403/404 remain restricted; transport failures and other
  statuses become `preview_unavailable`. Both unsuccessful access-check paths
  withhold protected evidence, including during initial navigation. For an existing session,
  unreported diagnostics remain available after a later successful recheck.
- `frontend/composables/useBlockGrouping.ts`: pending actions were findings.
  They now appear as neutral “Waiting for data,” including on the group header.
  A later completed check for the same action removes the pending indicator.
  Missing acknowledgements and inconclusive data still remain findings.
- `frontend/components/dashboard/ArtifactFrame.vue`: each delivery included all
  historical request IDs. It now includes only the latest accepted request for
  each dataset present in that payload, retains IDs across resends, and clears
  them when fetched snapshots replace the parameter-query results.

## Loop A — deterministic reproduction

Use Python 3.12 with the existing backend development dependencies and frontend
dependencies installed. No app database, credentials, or LLM calls are needed.
The browser boundary tests use installed Chromium; no browser download occurs.

```sh
cd backend
TESTING=true .venv/bin/python -m pytest --confcutdir=tests/unit tests/unit/test_artifact_browser_verification.py tests/unit/test_browser_tools.py -q
cd ../frontend
node --experimental-strip-types tests/unit/useBlockGrouping.mjs
node tests/unit/artifactVerificationDelivery.mjs
```

To independently reproduce the delivery regression without changing the checkout:

```sh
git show 87e1ac61b:frontend/components/dashboard/ArtifactFrame.vue > /tmp/pre-review-frame.vue
cd frontend
ARTIFACT_FRAME_SOURCE=/tmp/pre-review-frame.vue node tests/unit/artifactVerificationDelivery.mjs
```

Observed before: the second payload acknowledged `['1', '2']` while containing
only request `2`'s result. The same test passes on the fixed source. It also
covers resending after lost delivery, independent datasets, late superseded
responses, failed queries, and datasets removed from the payload.

The grouping regression failed before with `issueCount: 1` for pending work;
it passes after with no finding and one pending action. Follow-up success clears
that pending count; a missing acknowledgement becomes a finding instead.

## Loop B — UI evidence on the existing localhost frontend

The capture harness compiles the real ticker and browser-tool Vue components
and grouping function, then mounts seeded results using the existing localhost
frontend's styles. Icons are fixture substitutes; screenshots show component
states, not a newly generated report or a new live LLM verification run.
No new application server or database writes are required.

```sh
node docs/feedback-loops/internal-artifact-browser-verification/capture-review-ui.cjs after
```

Before/after English and Hebrew screenshots are in
`media/pr/internal-artifact-verification/review-{before,after}-{en,he}.png`.

## Privacy scope and rejected suggestions

Intended existing-connector behavior change: disabling `allow_llm_see_data`
removes **all browser tools** from the main agent's catalog, including connector
browsing. Screenshots and page text can disclose data too. The setting does not
delete the connection or prevent users from viewing artifacts themselves.

The two suggestions to add explicit Vue-i18n plural arguments are false
positives: this installed version already selects plural forms from named
`count`. Direct execution produced “1 finding,” “3 findings,” and “10 checks”;
adding the explicit argument produced identical results. Existing Hebrew
singular catalog wording is a separate translation issue.

## Scope limits

This loop verifies the affected session, access-check, delivery, and transcript
contracts. It is not a fresh natural-language generation run, a full application
QA pass, or verification of every generated app. Prior generated-app quality
findings and deployment limitations recorded in PR #1133 remain applicable.

## Final focused validation

Luna ran the final artifact/browser suite: **60 passed**, including real
Chromium checks. The existing browser-tool suite also passed: **27 passed**
(**87 total**, no failed or skipped tests in those two runs). Both frontend
Node regression scripts passed. The initial restricted Chromium launch failures
were environmental; the same cases passed with local browser access.

Logs: `internal-artifact-browser-verification/review-artifact-browser.txt`,
`review-browser-tools.txt`, `review-grouping.txt`, and `review-delivery.txt`.

Frontend production build: **passed (exit 0)**. The log is
`internal-artifact-browser-verification/review-frontend-build.txt`. It includes
existing duplicate-key/import and chunking warnings. Final localhost health
checks bypassing the proxy returned HTTP 200 for frontend and backend; no
application process was restarted.
