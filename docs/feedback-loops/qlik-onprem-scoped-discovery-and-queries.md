# Feedback loop — Qlik on-prem indexing and inspection failures

The customer reported whole-stream indexing failures and repeated `inspect_data`
failures followed by `create_data`, on approximately BOW 0.0.561. No customer
trace or live Qlik server was available. This loop establishes connector defects
independently; it does **not** establish that every reported failure had the same
cause, or that the customer's server ran out of memory.

## Root causes and changes

| Contract | Before | After |
|---|---|---|
| Discovery scope | Only stream/published filters; every matching app is opened in the Engine | Optional comma-separated app names/IDs intersect the other filters before capping or opening apps; unknown filters return no apps |
| Indexing load | Four concurrent opens, no form control | `max_concurrency` exposed as 1–4; default remains 4 for compatibility; choose 1 for large apps |
| Field selections | `SelectInField` called on a Doc handle, with every value converted to text | Shared `GetField` → `SelectValues` helper for on-prem and Cloud; numeric values remain numeric; rejected or invalid filters fail before cube creation |
| Query identity | GUID bypassed app metadata and used `sa_repository`, while names could use the app owner | IDs and names resolve the same scoped app row; an explicit configured/member identity still wins |
| Query target | A bad fully qualified path could fall back to another stream's bare app name | Full paths are preserved; unknown/ambiguous references fail explicitly |
| Inspection context | Instructions/resources were read but absent from the codegen prompt | Both are rendered; SQL-specific examples no longer override the connector's query API |

Navigation:

- `backend/app/data_sources/clients/qlik_sense_onprem_client.py`: `_apps_from_rows`,
  `get_schemas`, `_resolve_app`, `_acting_user`, `system_prompt`.
- `backend/app/data_sources/clients/_qix_common.py`: `apply_field_selections`.
- `backend/app/data_sources/clients/qlik_sense_client.py`: same selection helper.
- `backend/app/schemas/data_sources/configs.py`: `QlikSenseOnPremConfig`.
- `backend/app/ai/agents/coder/coder.py`: `generate_inspection_code`.
- `backend/tests/unit/test_qlik_query_contract.py`: HTTP/WebSocket-boundary
  tests that reject methods on the wrong object type instead of blindly
  returning canned success responses.

Protocol reference: [Qlik field-selection example](https://help.qlik.com/en-US/sense-developer/May2025/Subsystems/EngineAPI/Content/Sense_EngineAPI/DiscoveringAndAnalysing/MakeSelections/select-values-in-field.htm).
Opening an app still loads its data on demand; removing preload tasks does not
make Engine discovery memory-free. See [OpenDoc](https://help.qlik.com/en-US/sense-developer/May2026/Subsystems/EngineJSONAPI/Content/service-global-opendoc.htm).

## Loop A — no external services

Use Python 3.13 and the repo's dev dependencies:

```bash
cd backend
uv sync --frozen --extra dev
export TESTING=true
export BOW_DATABASE_URL=sqlite:///db/test-bootstrap.db
mkdir -p db
uv run pytest tests/unit/test_qlik_query_contract.py \
  tests/unit/test_sandbox_feedback_loop.py::TestErrorFeedbackReachesPrompts::test_inspection_preserves_business_instructions_and_resources \
  -q --disable-warnings
```

To reproduce before the fix, create a detached worktree at the base revision and
copy only the regression tests from this branch into it:

```bash
# From this branch's repository root; use a fresh destination.
git worktree add --detach /tmp/bow-qlik-before a74003f3a
cp backend/tests/unit/test_qlik_query_contract.py /tmp/bow-qlik-before/backend/tests/unit/
cp backend/tests/unit/test_sandbox_feedback_loop.py /tmp/bow-qlik-before/backend/tests/unit/
cd /tmp/bow-qlik-before/backend
# Run the setup and pytest command above in this worktree.
```

Observed on the base: **35 failed, 1 passed**. Failures include unknown
`SelectInField`, a different identity for a GUID, accepted ambiguous/wrong
paths, missing app-filter configuration and dropped inspection instructions.
The explicit member identity case already worked and remains a control.
On the fixed code, the same regression selection passed **36/36** (included
in the broader successful run below).

The focused verification command also covers both existing Qlik suites, query
wrapper compatibility, codegen feedback and relative dates:

```bash
uv run pytest tests/unit/test_qlik_query_contract.py \
  tests/unit/test_qlik_sense_onprem_client.py tests/unit/test_qlik_sense_client.py \
  tests/unit/test_query_timeout.py tests/unit/test_sandbox_feedback_loop.py \
  tests/unit/test_relative_date_prompts.py \
  tests/e2e/test_data_source.py tests/e2e/test_connection.py \
  --db=sqlite -q --disable-warnings
```

Observed after the fix: **241 passed, 0 failed, 0 skipped** (229 focused unit
tests and 12 data-source/connection API tests). No failing checks were
attributed away from this change. The registry also resolved the client, and
`git diff --check` passed. A full frontend production build was not run; the
actual Nuxt development app was used for the UI verification below.

## Loop B — UI and transport smoke

The actual frontend and backend were run with an isolated SQLite database and
a seeded QA organization. A local TLS QRS/WebSocket simulator served the
existing synthetic Qlik fixture, not customer data.

Observed:

1. Baseline form had only Stream Filter; updated form displayed App Filter and
   Concurrent Apps. English and Hebrew/RTL screenshots were inspected.
2. Test connection returned HTTP 200 with `success=true`.
3. Save returned HTTP 200 and persisted `app_filter="Consumer Sales"` and
   `max_concurrency=1`. Reading the connection back preserved both values.
4. Indexing returned exactly four tables under `Sales/Consumer Sales/`:
   Sales, Orders, Customers and Master Items. The table selector displayed only
   those four; selecting all and saving succeeded.
5. A generated-code stub was executed through the real
   `StreamingCodeExecutor.generate_and_execute_stream_v2` and the real client,
   over TLS/WebSocket to the simulator. It queried the full Orders path with a
   Region Name selection and returned the synthetic measure `Qty=42`, without
   errors. This covers the executor shared by inspect/create, not live LLM
   generation or a production Qlik calculation.

Screenshots: `media/pr/qlik-onprem-scoped-discovery/` (`before`, `after`,
`after-he`, `tested`, `indexed`). Credential controls in the test screenshot
are masked. No frontend component change was needed: the form uses the backend
schema and its existing locale-key support. Four keys were added to all ten
catalogs; existing missing/extra counts did not increase.

## Intentional behavior changes and limits

- A query's app must now resolve within the connection's published/stream/app
  filter scope. `max_apps` limits discovery only, not query resolution. A raw
  GUID no longer bypasses the filters or drops its owner metadata. Scoped,
  uncapped metadata is cached on the identity-bound client for 30 seconds;
  discovery refreshes it. Expired-cache QRS errors propagate instead of
  silently changing identity or reusing stale metadata.
- Empty filter lists, booleans, nulls and non-finite numbers are rejected.
  Omit a field from `filters` when it should not constrain the query. Text
  identifiers such as `"001"` remain text.
- No preload task is created, and no global default concurrency was changed.
  App filtering currently happens after the QRS metadata listing but before
  Engine opens. It reduces Engine work, not the size of the initial QRS reply.
- No automatic inspect→create retry or active-table bypass was added.
  `No active tables matched` is a BOW catalog-resolution failure, not proof
  that Qlik data is absent. Connector guidance preserves exact table paths and
  source IDs, rechecks scope before a corrected retry, and avoids inferring a
  data/business-logic defect from a connection failure.
- Real QSEoW/Cloud behavior, customer Section Access, cold-start latency and
  RAM consumption are **not verified**. Repeat a failing customer question
  after upgrading, with one selected app and concurrency 1, and retain both
  tool errors/generated code. Version 0.0.601's transient-disconnect/table
  retention fix (#1508) is a prerequisite for assessing the old deployment.

## Review follow-up

The five review threads were checked against the implementation and Qlik's
[current-selection protocol](https://help.qlik.com/en-us/sense-developer/3.0/Subsystems/EngineAPI/Content/DiscoveringAndAnalysing/MakeSelections/get-current-selections.htm).

- Both clients now read `qSelectionObject` after all selections and require a
  positive `qSelectedCount` for every requested field before creating a cube.
  A successful RPC with no selected values, including a dual-date display
  string that does not match, fails explicitly. A later filter clearing an
  earlier filter also fails. This verifies that filters are present, not that
  every value in a multi-value filter matched. Numeric/date values are never
  guessed; both connector prompts explain the typed-value contract.
- Query metadata is uncapped and cached for 30 seconds per client, preserving
  owner identity and ambiguity detection. Tests cover cache reuse, expiry,
  QRS failure after expiry, owner refresh, and queries outside the indexing cap.
- App-filter names are case-insensitive; GUIDs remain case-insensitive even in
  `GUID/Table` references. The zero-app connectivity message names both filters.
  Query names and full paths still require the canonical schema spelling.
- Concurrency uses the existing schema-driven select with numeric options 1–4.
  Decimal, empty and null values also remain rejected by backend validation.
- Inspection retains the supplied context. `InstructionContextBuilder` already
  applies relevance/on-demand and organization limits (while preserving
  `always` instructions); structured `ResourcesSection.render()` includes at
  most 10 full resources and 200 index entries per repository. Arbitrary
  inspection-only truncation would risk discarding business rules. Token cost
  still grows with repository count, large entries, and `always` instructions;
  this PR does not claim a hard token budget or measured latency improvement.

Reproduction: run `test_qlik_query_contract.py` with its updated tests but the
two production client/helper files from `e90d61840`: **11 failed, 39 passed**.
On the review fixes the same suite passes **50/50**. The broader command above
passes **256 tests, 0 failed, 0 skipped**. These are boundary simulations,
not confirmation of dual-value behavior on a real Qlik installation.

UI evidence: `review-input.png` uses the pre-review backend config schema and
shows the numeric input on the same page and viewport; `review-select.png`
and `review-select-he.png` show the real updated form in English/Hebrew.
Browser verification found exactly the options 1, 2, 3, 4 and selected 1.
