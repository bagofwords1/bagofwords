# Evals Harness Plan

## Goal

Reuse the in-product evals feature (`TestSuite` / `TestCase` / `TestRun` /
`TestResult`, plus `TestEvaluationService` and `Judge`) as the evaluation
harness. Add a YAML loader so suites can be versioned in git, consumed by CI
pytest evals, bootstrapped on startup, and imported by customers via API/CLI.

## Non-goals

- Building a parallel eval stack that duplicates the assertion engine or the
  agent execution path.
- Replacing the in-product UI for authoring suites — YAML is complementary.
- Shipping the customer-facing import endpoint in phase 1.

## Why reuse the product feature

- Assertion grammar (`ExpectationsSpec`, matchers, `OrderingRule`,
  `ToolCallsRule`, `FieldRule`) already exists and is pydantic-validated.
- Execution already wired to `AgentV2.main_execution` via
  `TestRunService.create_and_execute_background` and `stream_run`.
- `TestEvaluationService.build_final_snapshot` + `evaluate_final` already
  introspect `ToolExecution`, `AgentExecution`, `Completion`, and `Judge`.
- `TestRun.build_id` already ties each run to an `InstructionBuild` — free
  regression comparison across prompt versions.

## What is missing today

- No file-based suite definitions — cases live only in DB, created via UI/API.
- No headless CLI — the legacy `backend/bow-eval.py` drives the old REST flow
  and predates the evals feature; this plan supersedes it.
- `TestEvaluationService.build_final_snapshot` only extracts structured fields
  for `tool:create_data` and `tool:clarify`. Artifact-level FieldRule support
  (`tool:create_artifact`, `tool:edit_artifact`) is deferred; for now,
  artifact assertions go through `ToolCallsRule`, `OrderingRule`, and `Judge`.

## Architecture

### Layers

1. **YAML schema** — thin pydantic wrappers (`SuiteYaml`, `CaseYaml`) that
   embed the existing `PromptSchema` and `ExpectationsSpec` so all validation
   is automatic.
2. **Import service** — `TestSuiteImportService` under
   `backend/app/services/` with `import_yaml(...)` and `export_yaml(...)`.
3. **CLI** — `python -m app.evals import <path> --org <slug>`; thin wrapper
   over the service.
4. **Pytest integration** — `backend/tests/evals/` parametrizes over YAML
   cases, seeds via the import service, runs via existing
   `TestRunService`, polls for terminal status.
5. **HTTP endpoints (phase 3)** — `POST /api/tests/suites/import`,
   `GET /api/tests/suites/{id}/export`.
6. **Startup bootstrap (phase 3, optional)** — feature-flagged loader that
   imports `backend/evals/suites/*.yaml` into a configured org on boot.

### File layout

```
backend/evals/
  PLAN.md                        # this file
  suites/
    sanity_smoke.yaml
    sanity_dashboards.yaml
    sanity_clarify.yaml
backend/app/services/
  test_suite_import_service.py   # new (phase 1)
backend/app/schemas/
  suite_yaml_schema.py           # new (phase 1) — SuiteYaml, CaseYaml
backend/app/evals/               # new (phase 1) — `python -m app.evals`
  __init__.py
  __main__.py
backend/tests/evals/             # new (phase 2)
  conftest.py
  test_evals.py
```

### YAML schema

```yaml
name: <string>                     # unique per org
description: <string?>
data_source_slugs: [<slug>, ...]   # default attachments for all cases
cases:
  - name: <string>                 # unique per suite
    prompt:
      content: <string>
      mentions: [...]?
      mode: page | slides | null
      model: <provider>/<model>?   # resolved by service (no UUIDs)
    data_source_slugs: [<slug>, ...]?  # per-case override
    expectations:
      spec_version: 1
      order_mode: flexible | strict | exact
      rules: [...]                 # any Rule shape from ExpectationsSpec
```

**Portability rule — no UUIDs in YAML.** Data sources referenced by slug (or
name fallback), LLM model referenced by `<provider>/<model>` pair. The
resolver errors loudly if a slug is missing in the target org.

### Upsert strategy

- Suite matched by `(organization_id, suite.name)`.
- Case matched by `(suite_id, case.name)`.
- Cases present in DB but absent from re-imported YAML are soft-deleted so
  historical `TestResult` rows remain linkable.
- `import_yaml(..., strategy="replace")` hard-deletes removed cases (opt-in).

### Result interpretation

Pytest evals assert `result["status"] == "pass"`. A suite is **passing** if
every case passes; the eval CI job fails the build if any case fails.

## Phases

### Phase 1 — YAML + loader (internal)

- [ ] `SuiteYaml` / `CaseYaml` in `app/schemas/suite_yaml_schema.py`.
- [ ] `TestSuiteImportService.import_yaml` / `export_yaml` with slug
      resolution.
- [ ] `python -m app.evals import <path> --org <slug>` CLI.
- [x] Sanity YAMLs checked in under `backend/evals/suites/` (this change).
- [ ] Unit tests for round-trip (`import_yaml(export_yaml(x)) == x`).

### Phase 2 — pytest evals

- [ ] Committed fixture data source (seeded sqlite under
      `backend/tests/fixtures/data/eval_demo.sqlite`).
- [ ] `tests/evals/conftest.py` — eval org, LLM provider, data source,
      `wait_for_run` poller (timeout configurable, default 180 s).
- [ ] `tests/evals/test_evals.py` — `@pytest.mark.evals` parametrized over
      every YAML case (ids: `<suite>/<case>`).
- [ ] CI job `evals` that runs nightly with `ANTHROPIC_API_KEY` secret.

### Phase 3 — customer-facing & bootstrap

- [ ] `POST /api/tests/suites/import` (YAML body, multipart optional).
- [ ] `GET /api/tests/suites/{id}/export` — round-trip.
- [ ] Startup bootstrap flag (`EVALS_BOOTSTRAP_ORG_SLUG`) that loads
      `backend/evals/suites/*.yaml` on boot.
- [ ] Docs for customers: authoring suites in git, running via CLI.

### Phase 4 — assertion coverage

- [ ] `tool:create_artifact` and `tool:edit_artifact` entries in the test
      catalog (`app/schemas/test_expectations.py`).
- [ ] Matching snapshot extractors in
      `TestEvaluationService.build_final_snapshot` so FieldRule can assert on
      artifact `mode`, `code`, generated components, etc.
- [ ] Optional vision-judge rule for rendered artifact screenshots.

## Open questions

- **Data source slug**: use `DataSource.name` directly, or add a dedicated
  `slug` column with a migration?
- **Model reference**: `provider_name/model_id` string, or require a custom
  slug column on `LLMModel`?
- **PR gate vs nightly**: fail PRs on eval regression, or only report nightly
  signal until baselines stabilize? Suggest: nightly only for phase 2, revisit
  after 2 weeks of stable data.
- **Build diff UI**: reuse existing `build_id` on `TestRun` (compare two runs
  via current UI) or add a dedicated "suite X on build A vs B" view?
- **Legacy `backend/bow-eval.py`**: delete in phase 1 or leave until CLI is
  shipped? Suggest: delete once the new CLI covers parity.

## Sanity YAMLs shipped with this plan

- `suites/sanity_smoke.yaml` — minimal end-to-end check (`create_data` + row
  count).
- `suites/sanity_dashboards.yaml` — dashboard prompt coverage (relevant to
  PR #206); two cases using `ToolCallsRule`, `OrderingRule`, and `Judge`.
- `suites/sanity_clarify.yaml` — verifies ambiguous prompts route to the
  `clarify` tool with a reasonable question.

These three are intentionally small so they can be hand-validated before the
loader is implemented.
