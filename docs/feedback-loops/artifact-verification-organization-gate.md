# Feedback loop — organization-controlled artifact verification

Internal browser verification is available only when `allow_llm_see_data` is true,
the organization has enabled `enable_artifact_verification`, and the current
report contains an accessible, saved, non-deleted page artifact. An explicit
target must itself meet those conditions. Existing connector browsing requires
data visibility and an authorized, active, attached browser connector, independently
of the artifact-verification setting. No deployment switch is involved.

Verification now defaults ON for new organizations and configurations without
the setting. Explicit stored OFF values remain OFF. Data visibility, page
existence, and runtime authorization checks remain mandatory. The policy/API
regression covers the default, explicit opt-out, re-enable, and privacy denial.

## Root cause

At the preceding revision (`aca7cee23`), `agent_v2.py:806` advertised the internal
preview capability from `BOW_ARTIFACT_VERIFICATION_ENABLED`, without looking for
a page artifact. Browser tools consulted settings cached on the running agent,
and connector operations had no runtime privacy gate. Hints could recommend
browser navigation even while labeling verification unavailable.

The initial reproduction called `build_artifact_verification_hint` with
`code='useState(0)'` and `available=False`. Observed output:

```text
{'recommended': True, 'next_tool': 'browser_navigate', 'availability': 'unavailable'}
AssertionError: Unavailable verification still asks the planner to browse
```

The same call after the change returns `recommended=False`, `next_tool=None`,
and `availability='unavailable'`.

## Reproduce the policy and runtime contracts

Use Python 3.12 and the repository test environment. These tests use API-created
users, organizations, reports, artifacts and connectors in an isolated test DB;
they do not depend on the local user's data or external LLMs. Browser/HTTP
boundaries are controlled where a precise mid-operation change is required.

```bash
cd backend
TESTING=true .venv/bin/python -m pytest tests/e2e/test_artifact_browser_policy.py --db=sqlite -q
TESTING=true .venv/bin/python -m pytest --confcutdir=tests/unit tests/unit/test_artifact_browser_verification.py tests/unit/test_browser_tools.py tests/unit/test_query_params.py -q
```

The browser tests require an installed Playwright Chromium. On macOS, run them
outside the restricted process sandbox; no browser download or application server
is required. The normal PostgreSQL test leg is available via `--db=postgres` but
was not run for this follow-up.

## Implementation

- `backend/app/services/artifact_verification_policy.py` reads committed settings
  and access in a fresh session. It reuses the existing artifact/report permission
  decorator for owner, admin, project and sharing behavior. It reloads the user
  and rejects disabled human accounts and revoked session epochs.
- `refresh_browser_tool_catalog` in `backend/app/ai/tools/artifact_verification.py`
  refreshes browser availability before each main planner step. It preserves
  unrelated dynamic tools and closes this execution's preview when ineligible.
- All five public browser tools use `_browser_policy.py` to check authorization
  before operations and again before returning tool results. Revocation or an
  unavailable access check closes the session and withholds snapshots, images,
  extracted text, parameters and query evidence, including on failure paths.
- Creation/edit hints query the exact saved target and recommend verification
  only when it is both eligible and complex. Static dashboards retain their
  no-interactive-verification behavior.
- The existing organization settings schema/API/UI supplies **Verify data apps**,
  off by default, with the existing lab indicator. All ten locale catalogs contain
  its label and description. No migration is required.

## UI evidence and local check

The existing frontend at `localhost:3000` was used with the regular `app.db`.
The before screenshot used the existing backend. Its reload worker subsequently
stopped serving HTTP, so the after/flow checks routed the existing frontend's API
requests through the real FastAPI handlers using `httpx.ASGITransport` in-process.
No application server was started or restarted. This proves the UI/API/settings
path, not recovery of the pre-existing backend worker.

Observed, using the same report and user context throughout:

```text
UI enabled -> fresh saved-page eligibility true
Restored disabled -> fresh saved-page eligibility false
```

The setting was restored to off. English and Hebrew screenshots were visually
inspected. The first Hebrew check caught a misplaced translation namespace;
the corrected capture displays the translated label and RTL controls. All ten
catalogs now add exactly the two expected feature keys with no increase in drift.

- Before: `media/pr/internal-artifact-verification/settings-before.png`
- After: `media/pr/internal-artifact-verification/settings-after-viewport.png`
- Enabled: `media/pr/internal-artifact-verification/settings-enabled.png`
- Hebrew: `media/pr/internal-artifact-verification/settings-he.png`
- Toggle recording: `media/pr/internal-artifact-verification/settings-flow.gif`

## Bounds of the evidence

This follow-up does not rerun natural-language app generation, certify every
generated app, test future write/MCP operations, or run the entire application
regression suite. Prior parameter/date and browser contracts are included in the
focused regression command above. Existing report viewing and saved artifact
content do not depend on the new verification toggle.

## Observed validation

Luna ran **87 browser/artifact unit tests**, **71 query-parameter tests**, and
**9 policy/API cases**, all passing. The policy-sensitive startup/session tests
moved from fabricated unit contexts to API-created, persisted e2e contexts.
Temporarily removing all five browser guard decorators made the denial test and
mid-operation evidence-revocation test fail. Exact source bytes were restored in
`finally`, then the targeted checks passed again.

The broader frontend locale sweep was stopped after repeated failures:
**11 failed, 2 interrupted, 17 not run**. The missing smoke-page element coincided
with `localhost:3000/i18n-smoke` returning connection refused. This is an unresolved
local availability limitation, not a green result or a proven baseline defect.
No application worker was killed or restarted.
