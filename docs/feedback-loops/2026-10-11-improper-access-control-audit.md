# Feedback Loop — "Improper Access Control" (yearly security audit, 5 sub-issues)

The scanner reported five access-control gaps. Each was traced in code,
reproduced as a failing test on the unfixed tree, fixed, and re-run green.
Two scanner findings turned out to be broader than reported (#1, #3); one
part of the #2 hardening was rolled back because it broke a legitimate path.

| # | Scanner finding | Validated? | Actual hole |
|---|---|---|---|
| 1 | `/api/queries/{id}/run` non-owner execution | **Yes** | Builder mode let *any* `run_custom_code` member replace another user's query `default_step` — the code every viewer then runs. Sibling `/api/queries/steps/{id}/run` let any member rerun any step in the org into its shared snapshot. |
| 2 | Identity-param bypass in `query_params.py` | **Yes** | `verify_identity_binds_in_queries` passed if the value appeared *anywhere* in *any* query: a comment, the SELECT list, or a dummy `SELECT 'me@x'` next to an unscoped read. |
| 3 | `/api/entities/{id}/run` edits approved entities | **Yes** | `PUT` denies the owner of an *approved* entity (`EntityService._determine_update_type`), but `/run` accepts `code`/`title`/`status` and persists them — a side door around the approval lock. |
| 4 | `/api/evals/{name}.yaml` lacks `manage_evals` | **Yes** | Any member could dump any suite's cases. |
| 5 | `/api/agents/{name}.yaml` lacks authorization | **Yes** | Any member could dump any agent's full manifest. |

## Root cause (validated)

- #1 `backend/app/routes/query.py` `run_query_new_step` gated builder mode on
  `run_custom_code` only; the service sets `q.default_step_id` on success
  (`services/query_service.py`, end of `run_query_new_step`).
  `run_existing_step` had only `view_reports` (org-level, no object check).
- #2 `backend/app/ai/code_execution/query_params.py`
  `verify_identity_binds_in_queries` — `found = any(str(n) in haystack …)` over
  the concatenation of all queries.
- #3 `backend/app/routes/entity.py` `_require_entity_run_access` returned early
  for the owner regardless of `global_status`; `services/entity_service.py`
  `run_entity_with_update` then applies payload `title/code/status/...`.
- #4 `backend/app/routes/eval_yaml.py` `export_eval_yaml` never called the
  file's own `_require_manage_evals`; `suite_service.get_suite` is org-scoped only.
- #5 `backend/app/services/agent_yaml_service.py` `export` — lookup by name +
  dump, no permission check (whereas `apply` runs `_check_permissions`).

## Loop A — deterministic reproduction

```bash
cd backend && uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true; mkdir -p db
git stash push -- app          # unfixed tree
uv run pytest tests/unit/test_query_params.py \
  tests/e2e/rbac/test_edit_and_export_authority.py -q -rf
git stash pop
```

Observed on the **unfixed** tree — `18 failed, 79 passed` (every new security
test fails; the "must stay open" tests pass):

```
FAILED test_query_params.py::test_identity_verification_rejects_unscoped_reads[queries0..4 × 2 emails]   (10)
FAILED test_edit_and_export_authority.py::test_only_the_query_owner_can_replace_its_code[admin]
FAILED test_edit_and_export_authority.py::test_only_the_query_owner_can_replace_its_code[member]
FAILED test_edit_and_export_authority.py::test_only_the_report_owner_can_rerun_a_step_into_its_snapshot
FAILED test_edit_and_export_authority.py::test_owner_cannot_edit_an_approved_entity_through_run[code|title|status]  (3)
FAILED test_edit_and_export_authority.py::test_eval_yaml_export_requires_manage_evals
FAILED test_edit_and_export_authority.py::test_agent_yaml_export_requires_manage_on_the_agent
```

Concrete pre-fix responses (assertion payloads):

- #1 non-owner builder run → `200 {"query": {...}, "step": {...}}` (default step replaced)
- #1 non-owner `steps/{id}/run` → reached code execution instead of 403
- #3 owner of approved entity, `POST /run {"title": "Renamed by owner"}` → `200 {"title": "Renamed by owner", ...}`
- #4 plain member → `200 name: suite-59e9e98f …` (full suite YAML)
- #5 plain member → `200 name: agent_a9abba …` (full agent manifest)

## The fix

| # | Change |
|---|---|
| 1 | `routes/query.py`: `_require_query_owner` — builder runs need the query's creator or its report's owner (mutations are owner-only, as on the report routes). `steps/{id}/run` requires the step's report owner; unknown/cross-org step → 404. Viewer mode untouched. |
| 2 | `query_params.py`: every captured **table-reading SQL** query must carry the identity value **after a predicate keyword** (`WHERE/ON/HAVING/QUALIFY/USING`) once comments are stripped (quoted literals preserved). Statements with no `FROM` are exempt; non-SQL connector captures keep plain containment. |
| 3 | `routes/entity.py`: a run that also changes definition fields on an **approved** entity takes the non-owner bar (`create_entities` on every agent / `manage_entities`), matching `PUT`. Plain refresh and values runs unchanged. |
| 4 | `routes/eval_yaml.py`: `_require_manage_evals` → 403 (same bar as `/api/tests/suites/{id}/export`). |
| 5 | `services/agent_yaml_service.py` `export`: full admin or `manage` on the agent → else 403 (same bar as `apply`; UI export button is already gated on `canManageAgent`). |

Re-run on the **fixed** tree:

```
tests/unit/test_query_params.py + tests/e2e/rbac/test_edit_and_export_authority.py
  + tests/e2e/test_report_rerun_params.py                     → 107 passed
31 related e2e files (entities, queries, yaml, evals, params, identity,
  code visibility, viewer runs, RLS, route scoping)          → 325 passed
```

## What this proves / regression notes

- Each hole is closed at the API, for both directions of role (admin can't
  rewrite a member's query either — mutations stay owner-only).
- Non-regressions are asserted, not assumed: viewer-mode query runs stay open
  to non-owners; owners still refresh approved entities; admins still edit them
  through `/run`; agent managers and admins still export.
- **Rolled back:** an initial "fail closed when no query was captured" rule
  broke `test_report_rerun_params.py` — identity params applied in pandas with
  no SQL are a supported pattern. Zero-capture runs pass as before.
- **Residual risk (#2):** this is a heuristic backstop — `WHERE 'x'='x'`
  still satisfies it. True per-viewer isolation belongs in the database
  (RLS keyed on a session variable / per-user credentials); worth its own issue.
- Pre-existing unrelated unit failures (identical with the fix stashed):
  `test_sharepoint_onprem_client`, `test_obo_stored_login_recovery`,
  `test_openai_tool_call_ids`, `test_obo_background_database`,
  `test_query_timeout`, `test_sybase_source`, `test_discovery_progress_contract`,
  `test_artifact_feedback_loop`, `test_sandbox_feedback_loop`.
  `test_reasoning_effort_levels` fails only under `-n 8` (log-capture
  ordering) and passes in isolation.
