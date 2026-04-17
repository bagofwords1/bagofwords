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

No new services or routers. Extend the existing test-suite surface.

| Change                                              | Phase | Consumer                        |
| --------------------------------------------------- | ----- | ------------------------------- |
| `SuiteYaml` / `CaseYaml` pydantic wrappers          | 1     | service + tests                 |
| `TestSuiteService.import_yaml` / `export_yaml`      | 1     | routes + pytest                 |
| `POST /api/tests/suites/import` (in `routes/test.py`)| 1    | customers + pytest via client   |
| `GET  /api/tests/suites/{id}/export`                | 1     | customers (round-trip)          |
| `backend/evals/suites/*.yaml`                       | 1     | pytest (canonical cases)        |
| `tests/evals/conftest.py` fixtures                  | 2     | pytest                          |
| `tests/evals/test_evals.py`                         | 2     | pytest                          |
| Artifact FieldRule extractors                       | 3     | richer assertions               |

### File layout

```
backend/evals/
  PLAN.md                              # this file
  suites/
    sanity_smoke.yaml
    sanity_dashboards.yaml
    sanity_clarify.yaml
backend/app/schemas/
  suite_yaml_schema.py                 # SuiteYaml, CaseYaml (phase 1, new)
backend/app/services/
  test_suite_service.py                # +import_yaml/+export_yaml (phase 1, edit)
backend/app/routes/
  test.py                              # +2 handlers (phase 1, edit)
backend/tests/evals/
  __init__.py
  conftest.py                          # loader, wait_for_run (phase 2)
  test_evals.py                        # parametrized over YAML (phase 2)
```

### YAML schema

Two shapes per case — single-turn (shown first) and multi-turn. Multi-turn
is **YAML-only**; the existing in-product UI continues to render turn 1 from
`prompt_json` and is blind to the extra turns.

```yaml
name: <string>                       # unique per org
description: <string?>
data_source_slugs: [<slug>, ...]     # default attachments for all cases
cases:
  # --- single-turn ---
  - name: <string>                   # unique per suite
    prompt:
      content: <string>
      mode: chat | deep | training | null   # default "chat"
      model: <provider>/<model>?     # resolved by service; no UUIDs
    data_source_slugs: [<slug>, ...]?  # per-case override
    expectations:
      spec_version: 1
      order_mode: flexible | strict | exact
      rules: [...]                   # any Rule from ExpectationsSpec

  # --- multi-turn (YAML-only) ---
  - name: clarify_then_answer
    turns:                           # mutually exclusive with `prompt:`
      - prompt: { content: "Show me the data" }
      - prompt: { content: "Users per month for 2025" }
    expectations:                    # evaluated against the full trace
      rules:
        - { type: tool.calls, tool: clarify, min_calls: 1 }
        - { type: tool.calls, tool: create_data, min_calls: 1 }
        - type: ordering
          mode: flexible
          sequence:
            - { tool_or_bind: clarify }
            - { tool_or_bind: create_data }
```

A case is multi-turn iff `turns` is present and non-empty. Schema validates
that exactly one of `prompt` or `turns` is set.

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

### Phase 1 — YAML on the existing test-suite surface

- [ ] `SuiteYaml` / `CaseYaml` in `app/schemas/suite_yaml_schema.py`
      (re-embed existing `PromptSchema`, `ExpectationsSpec`).
      Validator: exactly one of `prompt` or `turns` per case.
- [ ] Extend **`TestSuiteService`** with `import_yaml(...)` / `export_yaml(...)`
      (slug resolution, upsert by name, soft-delete removed cases).
      Reuse `TestCaseService` internals for case persistence — no duplication.
- [ ] Add two handlers to **`routes/test.py`**:
      - `POST /api/tests/suites/import` — body: YAML string (or file upload).
      - `GET  /api/tests/suites/{id}/export` — returns YAML.
- [ ] **Multi-turn support (YAML-only, not in UI)**:
  - [ ] Alembic migration: add nullable `additional_turns_json` column on
        `test_cases` (list of `{prompt: PromptSchema}`; turn 1 keeps living
        in `prompt_json` for UI backward-compat).
  - [ ] Importer splits `turns[0] → prompt_json`, `turns[1:] →
        additional_turns_json`. Export reassembles.
  - [ ] `TestRunService.create_and_execute_background` (and `stream_run`)
        iterate turns: after turn N's agent reaches a terminal state, create
        a follow-up head completion on the same `Report` (parent =
        previous system completion) and launch the next agent run.
  - [ ] Evaluator unchanged — `build_final_snapshot` already scans the full
        report, so global expectations cover multi-turn. Per-turn scoped
        assertions are deferred.
  - [ ] UI renders turn 1 only (reads `prompt_json` as today). No frontend
        changes required; optional small badge for "multi-turn (N)" can come
        later.
- [ ] Unit tests: round-trip, slug resolution errors, upsert preserves IDs,
      multi-turn threading produces N `AgentExecution` rows.
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

### Phase 3 — richer assertions: phase scoping + artifact + instruction

Extends the evaluator's rule grammar and snapshot surface. No harness or
import changes.

**Why phase scoping.** Without it, `tool.calls create_instruction min_calls: 1`
can't tell you *which* agent loop produced the call. The knowledge harness is
gated by `trigger.py` — in `mode: chat` it only runs when triggered; in
`mode: training` it's skipped entirely and instruction tools surface in the
main loop. Evals need to pin assertions to the loop they're evaluating, and
need a primitive that says "assert the knowledge harness actually fired."

**Per-rule `phase` filter** (optional, default `any`, backward compatible):

```yaml
- type: tool.calls
  tool: create_instruction
  phase: knowledge          # main | knowledge | any
  min_calls: 1

- type: ordering
  phase: knowledge
  sequence:
    - {tool_or_bind: search_instructions}
    - {tool_or_bind: create_instruction}

- type: field
  phase: knowledge
  target: {category: "tool:create_instruction", field: text}
  matcher: {type: text.contains, value: "exclude cancelled"}
```

**Per-rule `turn` filter** — 1-indexed turn number; omit for any turn.
Works on every rule type (including `PhaseRule`) and combines with `phase`:

```yaml
- type: tool.calls
  tool: clarify
  turn: 1                   # must happen in turn 1 specifically
  min_calls: 1

- type: phase
  phase: knowledge
  turn: 2                   # knowledge harness must fire during turn 2
  occurred: true
```

Backed by a `Completion.turn_index`-ordered ranking of system completions;
turn 1 = the earliest agent run on the report, regardless of the absolute
`turn_index` value.

**`PhaseRule`** — dedicated primitive for phase-presence:

```yaml
- type: phase
  phase: knowledge
  occurred: true            # fail if no PlanDecision.phase == "knowledge_harness"
```

**New catalog categories** (with `build_final_snapshot` extractors):

- `tool:create_artifact` → `mode` (page|slides), `visualization_ids` (list),
  `title`.
- `tool:edit_artifact` → input/output fields relevant for edit flows.
- `tool:create_instruction` → `text`, `category`.
- `tool:edit_instruction` → `text`.
- `tool:search_instructions` → `query`.

**Data path.** `ToolExecution.plan_decision_id` → `PlanDecision.phase`
(nullable string, "main" | "knowledge_harness"). No migration.
`build_final_snapshot` adds parallel arrays `tool_phases` and `phases_seen`;
evaluator normalizes `"knowledge_harness" → "knowledge"` when filtering.

**Use patterns** this unlocks:

- Instruction authoring via `mode: training` — `tool.calls
  create_instruction min_calls: 1` (no phase needed; lives in main).
- Knowledge-harness reflex via `mode: chat` multi-turn with a correction
  in turn 2 — `phase: knowledge, tool.calls create_instruction min_calls: 1`
  plus `{type: phase, phase: knowledge, occurred: true}`.

**Checklist:**

- [ ] Add `phase` field to `ToolCallsRule`, `OrderingRule`, `FieldRule`.
- [ ] Add `PhaseRule` to the `Rule` union.
- [ ] `build_final_snapshot`: join `ToolExecution → PlanDecision.phase`;
      populate `tool_phases`, `phases_seen`, and category-specific extractors
      for the five new tool categories.
- [ ] `evaluate_final`: filter per-rule by phase; handle `PhaseRule`; handle
      new categories.
- [ ] Sanity YAML exercising knowledge-harness trigger (committed this change).
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
