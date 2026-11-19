# Feedback loop — nested Qlik measure choices

A chart can read a wrapper variable whose definition selects a measure. A query-local override must reach that selector, preserve the source formula, and leave the application's default unchanged. All examples and data in this document come from the synthetic Qlik test fixture.

## Root cause

Direct substitution does not reach a selector hidden behind another variable. In the fixture, the chart reads `$(vWrappedMetric0)` while its definition contains `Pick(Match(vMetric,'Sales','Quantity'), ...)`. Replacing `vMetric` only in the chart expression leaves the default active.

Evaluated definitions can also return formula strings. They require Qlik's evaluation boundary, not just textual removal of the leading `=`. Separately, fixed limits can hide later options or referenced definitions from agent context.

## Deterministic reproduction

Use Python 3.13 in a fresh checkout:

```bash
cd backend
uv sync --frozen --extra dev
mkdir -p db
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db uv run pytest \
  tests/unit/test_qlik_nested_variables.py \
  tests/unit/test_qlik_onprem_dynamic_measures.py \
  tests/unit/test_qlik_onprem_sheet_objects.py \
  tests/unit/test_qlik_query_contract.py \
  tests/unit/test_qlik_sense_client.py \
  tests/unit/test_qlik_sense_onprem_client.py -q --tb=short --disable-warnings
```

The external fake Engine is the only mocked service. Tests call public `get_schemas()` and `execute_query()` methods and the real schema-context serializer. They vary wrapper depth, selector names, branch counts, control property shapes, and formula size. Results are compared with independently evaluated expressions from the synthetic fixture.

For the baseline, create a separate worktree at #1774's implementation commit (`f1a5d14fc40400232d308bf448b14c3f7590fb29`), copy the current regression test and external fake fixture into it, and run the same tests. The initial 24 regression cases produced **19 failures and 5 passes** against that implementation, then all passed with this fix. Two additional large-formula cases failed against the first revision of this fix and passed after compact references were added. The final focused suite produced **285 passes**.

## Fix and code map

| Responsibility | Location |
| --- | --- |
| Transitive discovery, bounded expansion, native evaluation boundaries | `backend/app/data_sources/clients/_qix_common.py`: `variable_dependencies`, `expand_variable_dependencies` |
| Normalize literal controls; retain unresolved control expressions explicitly | `_qix_common.py`: `summarize_switch` |
| Preserve choices without repeating long formulas | `_qix_common.py`: `object_options`, `describe_options` |
| Read current Engine definitions and validate requested overrides | `backend/app/data_sources/clients/qlik_sense_onprem_client.py`: `_resolve_cube_variables` |
| Preserve referenced definitions beyond generic catalog limits | `qlik_sense_onprem_client.py`: `_build_master_table` |
| Public-contract regressions | `backend/tests/unit/test_qlik_nested_variables.py` |
| Independent external-service evaluator | `backend/tests/fixtures/fake_qlik_engine.py` |

Only affected dependencies are expanded. Evaluated definitions retain native `$(=...)` boundaries. Unused overrides, cycles, excessive nesting, and affected parameterized macros fail instead of silently returning a default. No variable setters or persistent application writes are introduced.

Explicit Pick/Match branches can supplement missing readable controls. They are labeled as expression branches, not verified UI choices. Nonliteral expression-valued controls remain unresolved rather than being advertised as literal arguments.

## Regression found while validating the fix

Repeating complete expanded formulas for every option made schema context too large. The final representation retains every choice and its execution arguments, keeps full shared definitions in Master Items, and references long formulas rather than duplicating them on each chart. Complete expanded options remain in metadata. The large-formula regressions verify both choice retention and reduced repetition.

Parsing caches are bounded and keyed by expression text, definitions, and choices. They do not cache query results.

## Scope and limits

The deterministic loop verifies discovery, agent-visible choices, query-local switching, unchanged defaults, and absence of application writes. A separate live end-to-end pass verified discovery and execution through the normal agent path; no live identifiers, formulas, values, logs, or screenshots are included here.

This is not exhaustive Qlik UI parity. Arbitrary expression-valued control enumeration, affected parameterized macros, all combinations of controls, and every extension object remain outside the demonstrated coverage. Existing deployments need a schema refresh to receive the revised descriptions.
