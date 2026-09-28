# Feedback loop — tool-side context reads (schema resolve, loadables, snippets)

**PR-B of the agent_v2 performance stack** (items 8, 9 and 5 of the plan).

Inside each `create_data` call, the tool rebuilt context the agent already
had:

- **Table resolution.** Every `tables_by_source` group ran a full
  `schema_builder.build` against the database. The run's
  `context_view.static.schemas` already holds the same tables, built by the
  same builder for the same user with the same active filter.
- **load_step discovery.** Up to 25 report steps were hydrated with their
  full result data and eager graph (34–42 statements), only to read column
  names and row counts. `Step.context_summary_json` already stores both.
- **Code snippets.** These were recomputed on every codegen retry.

Some of those reads also used the shared `AsyncSession` **without**
`_tool_db_lock` while parallel sibling tools used it: the viz instruction
build, loadables discovery, and the inspect_data excerpt. On Postgres, that
is a real race, observed live in the baseline (S4):

```
create_data._resolve_active_tables raised for ['Invoice'] (ds_id=None)
sqlalchemy.exc.InvalidRequestError: This session is provisioning a new
connection; concurrent operations are not permitted
```

The tool then failed with "Table resolution failed due to an internal
error", and the planner had to retry it.

## Change

| where | before | after |
|---|---|---|
| `CreateDataTool._resolve_active_tables` | `schema_builder.build` per group | `_resolve_group_from_static` from the run's schema context when **every** requested table is present; any miss falls back to the build (the static context is top-k capped) |
| `LoadablesResolver.list_for_discovery` | hydrate 25 steps + data + eager graph | `lazyload("*")` + `defer(Step.data)`; columns and row counts from `context_summary_json`; column query only for legacy summary-less steps |
| `prompt_formatters` loadables section | shared session | short-lived `read_session_maker` session |
| `Coder` snippets | per codegen retry, shared session | once per tool call, own read session |
| viz instruction build, `load_step` / `load_entity` resolution, inspect_data excerpt | shared session, no lock | under `_tool_db_lock` |

## Offline replays (real data, old vs new code)

| replay | cases | result |
|---|---|---|
| table resolution: old build vs static resolve | 16 (case-insensitive, cross-source, schema-qualified, missing tables) | **identical groups**; 8 → 0 queries when static hits; misses fall back |
| loadables discovery section text | 12 real reports | **identical text**; 34–42 → 2 statements |
| coder schema excerpt | unchanged path | byte-identical |

## Loop — real UI scenarios (before = PR-A, after = PR-A + PR-B)

The harness, gates and scenarios are the same as in
`agent-persistence-single-commit.md`.

| env · scenario | metric | before | after | Δ |
|---|---|---|---|---|
| P0 · S3 (with data source) ×3 | whole run | 43.7s | **33.3s** | −24% |
| | parallel tool batch | 20.0s | **12.3s** | −39% |
| | `schema_builder.build` calls / run | 7 | **4** | −3 (one per tool) |
| P0 · S1 ×3 | whole run · batch | 41.1 · 18.2s | 39.9 · 17.9s | ≈ |
| P0 · S2 ×2 | whole run | 11.1s | 10.8s | ≈ |
| P0 · S5 (200k CSV) | whole run | 26.1s | 18.8s | single run |
| P1 · S3 ×2 | whole run | 68.3s | **53.6s** | −22% |
| | parallel tool batch | 28.4s | **20.2s** | −29% |
| | max event-loop lag | 328 ms | 261 ms | |
| P1 · S1 ×2 | whole run · batch | 63.5 · 28.2s | 66.5 · 31.3s | within LLM variance (range 62–70s) |
| | `schema_builder.build` time / run | 3243 ms | **2044 ms** | −37% |
| SQLite · S3 | whole run · batch | 34.6 · 12.9s | **27.6 · 7.6s** | −20% · −41% |
| SQLite · S6 | steer applied · Stop | 1143 · 1396 ms | 1020 · 1245 ms | |
| P0 · S4 follow-up | whole run | 14.9s | 24.1s | the planner made 3 tool calls and 11 refreshes (vs 2 and 7), so it is not comparable |

Correctness: **18/18 runs passed all gates**, with:
- 84 event→fetch checks, 0 mismatches;
- table usage 16/16;
- 0 new log errors.

The concurrent-session error seen in the baseline did not recur in any PR-A
or PR-B run.

## Unit tests

`backend/tests/unit/test_tool_context_reads.py` covers:
- static resolution across cases and data sources;
- fallback when any table is missing from the static context;
- discovery reading summaries without hydrating any step's result data,
  including a legacy step with no summary.

## Not verified

- A data source with more tables than the static top-k: the code falls back
  to the build, which a unit test covers. The demo data source here fits
  within top-k.
