# Evals Harness Plan

## Goal

Two shipments, one loader:

1. **Customer feature** — YAML import/export via HTTP so customers can version
   test suites in git and push them to their org.
2. **Internal pytest evals** — a handful of YAML suites under
   `backend/evals/suites/` that run through pytest like the existing e2e
   tests (`test_report.py`, `test_eval.py`), consuming the same HTTP endpoint
   via `test_client`.

The import service is the seam that both consumers share.

## Non-goals

- A CLI (`curl` / `test_client` / the UI cover every use case).
- Replacing the in-product UI suite editor — YAML is complementary.
- Building a parallel assertion engine. All rules go through the existing
  `ExpectationsSpec` / `TestEvaluationService` / `Judge` pipeline.

## Why reuse the in-product feature

- `ExpectationsSpec`, matchers, `OrderingRule`, `ToolCallsRule`, `FieldRule`
  already pydantic-validated.
- `TestRunService.create_and_execute_background` already drives real
  `AgentV2.main_execution` per case.
- `TestEvaluationService.build_final_snapshot` + `evaluate_final` already
  introspect `ToolExecution`, `AgentExecution`, `Completion`, `Judge`.
- `TestRun.build_id` already ties runs to `InstructionBuild` — free
  regression comparison across prompt versions.

## Architecture

### Components

| Component                                | Phase | Consumer                         |
| ---------------------------------------- | ----- | -------------------------------- |
| `SuiteYaml` / `CaseYaml` pydantic        | 1     | service + tests                  |
| `TestSuiteImportService`                 | 1     | HTTP route + pytest              |
| `POST /api/tests/suites/import`          | 1     | customers + pytest (via client)  |
| `GET  /api/tests/suites/{id}/export`     | 1     | customers (round-trip)           |
| `backend/evals/suites/*.yaml`            | 1     | pytest (canonical cases)         |
| `tests/evals/conftest.py` fixtures       | 2     | pytest                           |
| `tests/evals/test_evals.py`              | 2     | pytest                           |
| Startup bootstrap (optional)             | 3     | self-hosted installs             |
| Artifact FieldRule extractors            | 4     | richer assertions                |

### File layout

```
backend/evals/
  PLAN.md                              # this file
  suites/
    sanity_smoke.yaml
    sanity_dashboards.yaml
    sanity_clarify.yaml
backend/app/schemas/
  suite_yaml_schema.py                 # SuiteYaml, CaseYaml (phase 1)
backend/app/services/
  test_suite_import_service.py         # import_yaml / export_yaml (phase 1)
backend/app/routes/
  test.py                              # +2 handlers (phase 1)
backend/tests/evals/
  __init__.py
  conftest.py                          # loader, wait_for_run (phase 2)
  test_evals.py                        # parametrized over YAML (phase 2)
```

### YAML schema

```yaml
name: <string>                       # unique per org
description: <string?>
data_source_slugs: [<slug>, ...]     # default attachments for all cases
cases:
  - name: <string>                   # unique per suite
    prompt:
      content: <string>
      mentions: [...]?
      mode: page | slides | null
      model: <provider>/<model>?     # resolved by service; no UUIDs
    data_source_slugs: [<slug>, ...]?  # per-case override
    expectations:
      spec_version: 1
      order_mode: flexible | strict | exact
      rules: [...]                   # any Rule from ExpectationsSpec
```

**Portability rule — no UUIDs in YAML.** Data sources by slug (or name
fallback); models by `<provider>/<model>` pair. Resolver errors loudly when
the target org is missing a referenced slug.

### Import semantics

- Suite matched by `(organization_id, suite.name)`.
- Case matched by `(suite_id, case.name)`.
- Cases present in DB but absent from the re-imported YAML are **soft-deleted**
  so historical `TestResult` rows remain intact.
- `strategy="replace"` hard-deletes removed cases (opt-in; query param).

### Pytest flow (mirrors existing e2e pattern)

```python
@pytest.mark.evals
@pytest.mark.parametrize("case_spec", load_all_yaml_cases(),
                         ids=lambda c: f"{c.suite}/{c.case}")
def test_eval_case(case_spec,
                   create_user, login_user, whoami,
                   create_llm_provider, create_data_source,
                   import_yaml_suite,            # new fixture (POSTs via test_client)
                   create_test_run,              # existing
                   wait_for_run):                # new fixture
    user = create_user(); token = login_user(...); org_id = whoami(...)[...]
    create_llm_provider(...)
    create_data_source(name="eval_demo", ...)   # fixture sqlite
    imported = import_yaml_suite(case_spec.suite_yaml_path, token, org_id)
    case_id = imported["cases_by_name"][case_spec.case]
    run = create_test_run(case_ids=[case_id], ...)
    result = wait_for_run(run["id"], timeout=180)
    assert result["status"] == "pass", result.get("failure_reason")
```

Same shape as `test_report.py` etc.

### Result interpretation

Pytest evals assert `result["status"] == "pass"`. Suite passes iff every case
passes. CI job `evals` fails if any case fails.

## Phases

### Phase 1 — YAML import service + HTTP endpoints

- [ ] `SuiteYaml` / `CaseYaml` in `app/schemas/suite_yaml_schema.py`
      (re-embed existing `PromptSchema`, `ExpectationsSpec`).
- [ ] `TestSuiteImportService.import_yaml` / `export_yaml`
      (slug resolution, upsert by name, soft-delete removed cases).
- [ ] Route handlers:
      - `POST /api/tests/suites/import` — body: YAML string (or file upload).
      - `GET  /api/tests/suites/{id}/export` — returns YAML.
- [ ] Unit tests: round-trip, slug resolution errors, upsert preserves IDs.
- [x] Sanity YAMLs checked in under `backend/evals/suites/` (this change).

### Phase 2 — pytest evals

- [ ] Committed fixture data source (`tests/fixtures/data/eval_demo.sqlite`).
- [ ] `tests/evals/conftest.py`:
      - `load_all_yaml_cases()` → list of case specs for parametrize.
      - `import_yaml_suite` → POSTs YAML via `test_client`.
      - `wait_for_run(run_id, timeout)` → polls `/api/tests/runs/{id}`.
- [ ] `tests/evals/test_evals.py` — single parametrized test.
- [ ] `@pytest.mark.evals` in `pytest.ini` + nightly CI job with
      `ANTHROPIC_API_KEY` secret.

### Phase 3 — bootstrap + docs (optional)

- [ ] Startup bootstrap flag `EVALS_BOOTSTRAP_ORG_SLUG` that imports
      `backend/evals/suites/*.yaml` on boot for self-hosted installs.
- [ ] Customer docs: authoring suites, import API reference, round-trip via
      `GET /export`.

### Phase 4 — artifact FieldRule coverage (independent)

- [ ] Add `tool:create_artifact` / `tool:edit_artifact` to the test catalog
      in `app/schemas/test_expectations.py`.
- [ ] Extend `TestEvaluationService.build_final_snapshot` to extract artifact
      `mode`, `code`, generated components from `ToolExecution`.
- [ ] Optional vision-judge rule over artifact screenshots.

Until this lands, dashboard-style assertions use `ToolCallsRule` +
`OrderingRule` + `Judge` (as `sanity_dashboards.yaml` does).

## Open questions

- **Data source slug**: reuse `DataSource.name`, or add a dedicated `slug`
  column? Same question for LLM model reference.
- **Import authn**: require admin role, or any org member with the existing
  test-suite permissions?
- **Replace vs upsert default**: default to upsert (safer), `?strategy=replace`
  for full sync.
- **PR gate vs nightly**: start nightly-only for phase 2, revisit after two
  weeks of stable baselines.
- **Legacy `backend/bow-eval.py`**: delete once `POST /import` + pytest evals
  cover its responsibilities.

## Sanity YAMLs shipped with this plan

- `suites/sanity_smoke.yaml` — minimal end-to-end check (`create_data` + row
  count).
- `suites/sanity_dashboards.yaml` — dashboard prompt coverage (relevant to
  PR #206); two cases using `ToolCallsRule`, `OrderingRule`, and `Judge`.
- `suites/sanity_clarify.yaml` — verifies ambiguous prompts route to the
  `clarify` tool with a reasonable question.
