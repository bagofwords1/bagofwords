# Feedback Loop — saved date filters rejected by stricter range validation

PR #1133 added date-range validation in the shared parameter resolver, which also
runs every existing saved viewer query. Valid individual ISO bounds accompanied
by metadata, mixed date/timestamp conventions, or a reversed order were accepted
by the prior resolver but now raised errors. This violated the requirement to
keep existing apps compatible.

## Root cause

`backend/app/ai/code_execution/query_params.py:163`, `coerce_param_value`, rejected
extra keys and compared range endpoint kinds/order globally. Query execution
reaches it through `resolve_param_values`, independently of artifact generation
or browser verification. Testing selected saved apps did not cover these legacy
input contracts.

The review reproduced these changes against base `196146240` using the public
resolver, without executing queries or modifying the application database:

| Input | Base | Before this fix |
|---|---|---|
| `{from, to, preset: 'last30'}` | Keeps bounds, discards metadata | `ParamError` |
| Calendar date lower bound, ISO timestamp upper bound | Preserves both | `ParamError` |
| Reversed individually valid bounds | Preserves order | `ParamError` |

Acceptance of reversed bounds never guaranteed zero rows: actual behavior belongs
to the saved query code. The compatibility fix must not silently reorder them.

## Correction

- Shared coercion again ignores ancillary keys and preserves valid bounds,
  including original timestamp precision, offsets, mixed conventions, and order.
  Request values and saved declaration defaults use the same contract. This
  preserves their existing resolved values and parameter fingerprints.
- The explicitly selected `calendar_date_bounds` helper remains calendar-only,
  rejects reversed days, and returns an exclusive next-day upper bound. It does
  not convert timezones. No existing query is silently migrated onto the helper.
- Verification emits inconclusive `mixed_date_range`, `reversed_date_range`, or
  `invalid_date_range` diagnostics for problematic declared ranges. Those checks
  are evidence for the agent, not a new rejection in ordinary viewer execution.
  Positive row counts and matching transport parameters alone do not prove a
  questionable range is correct.
- Generation guidance tells new date pickers to keep draft bounds locally and
  commit the complete valid range together, avoiding invalid intermediate calls.
- Invalid scalar dates/timestamps, unknown top-level parameter names, identity
  bindings and SQL rendering guards remain validated. An object with neither
  `from` nor `to` is still invalid. Extra metadata is discarded, never forwarded
  into SQL or interpreted as another bound.

## Reproduce and verify

Use Python 3.12 and the repository's existing backend development dependencies.
Run from `backend` so configuration resolves the repository VERSION correctly.
These tests use isolated boundaries and do not mutate the normal app database:

```sh
cd backend
TESTING=true .venv/bin/python -m pytest --confcutdir=tests/unit \
  tests/unit/test_query_params.py \
  tests/unit/test_artifact_browser_verification.py \
  tests/unit/test_browser_tools.py -q
```

The browser tests require installed Chromium; they do not install it. The date
resolver and evidence checks themselves require no browser, database, credentials
or LLM. Regression coverage includes legacy defaults and request inputs, metadata
stripping, timestamp precision and UTC-offset ordering, calendar-helper guards,
and inconclusive verification even when a query returns rows successfully.

## Scope

No schema migration, rewrite of saved app/query code, or frontend component
change. This restores the three reviewed legacy input behaviors; it does not
claim every existing generated app has been audited. Verification does not infer
source timezone semantics or reinterpret a legacy query's endpoint convention.

## Observed validation

Luna ran the new regression cases before implementation: **9 failed, 9 passed**
(112 unrelated cases deselected). The failures included rejected legacy metadata
defaults and missing range diagnostics for successful queries.

After the fix, the complete focused files passed: **71 parameter-contract tests,
64 artifact/browser tests, and 27 existing browser-tool tests — 162 passed**.
The real Chromium cases ran; none were counted as skipped. Logs are in
`internal-artifact-browser-verification/date-compat-query-params.txt`,
`date-compat-artifact-browser.txt`, and `date-compat-browser-tools.txt`.

All changed Python modules parsed successfully. No frontend components changed,
so this follow-up did not repeat the preceding commit's production frontend
build or screenshot capture. No new LLM generation run was performed.
