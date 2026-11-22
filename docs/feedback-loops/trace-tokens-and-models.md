# Feedback Loop — "which step used up the context, and which model ran it?" (BOW-216)

An org admin opens the trace modal on a slow or expensive run. Today it shows
one token total per turn: no split between input and output, no number per
planner step, and no model. The agent itself can't answer "tokens per turn in
this chat" either, because `bow.runs` always leaves the current conversation
out. The claim: the data already exists (planner token usage per decision,
the rollup's primary model, per-call usage records). This change only has to
normalize and expose it, and let a training conversation name itself.

## Root cause (validated)

- **Per-step tokens exist but are unused.** `PlanDecision.metrics_json.token_usage`
  is written by PlannerV3 (`backend/app/ai/agents/planner/planner_v3.py:650-670`)
  and serialized on every block (`backend/app/serializers/completion_v2.py:362`).
  `buildStepTimings` in `frontend/components/console/TraceModal.vue` read only
  its timing fields.
- **Raw `prompt_tokens` is not comparable across providers.** Anthropic reports
  cache reads and writes beside it, while OpenAI-format clients include them.
  Validated live below: a Haiku 5.5 step had `prompt_tokens` 1,440 with
  79,873 cache-read tokens, so the real input was 81,313. The rule is
  `pricing.cache_inside_prompt_tokens` (`backend/app/ai/llm/pricing.py`). The
  trace payload carried no provider, so the frontend could not apply it.
- **The model was never sent.** `AgentExecution.primary_model_id` /
  `primary_provider` (`backend/app/models/agent_execution.py:73-74`) were
  missing from `AgentExecutionSchema`.
- **The current conversation was always excluded.** `BowClient` passes
  `exclude_report_id=self._report_id` (`backend/app/data_sources/clients/bow_client.py`),
  and `bow_source_service.py` applied it unconditionally. The agent also had no
  way to know its own report id.
- **`model:` matches only the planner model.** It compiles to
  `AE.primary_model_id` (`backend/app/services/diagnosis/compiler.py`), so a
  run whose codegen used another model could not be found by that model.

## Loop A — deterministic (pytest, no LLM)

Runs are seeded with planner decisions and usage records (`seed_agent_executions`
now takes `decisions=[...]` and `routed`/`baseline_model` on usage rows).

```bash
cd backend
uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/rbac/test_trace_tokens_and_models.py \
  tests/e2e/test_bow_current_report.py \
  "tests/e2e/test_diagnosis_explorer.py::test_field_predicates" --db=sqlite -n 4
```

With `backend/app` stashed (before the fix):

```
19 failed, 11 passed
```

The 11 that pass are guards, and they are meant to pass on both sides: broad
queries keep excluding the conversation, scoped managers and members are still
refused, and a run whose provider is unknown gets no guessed input.

What the tests assert:

| Test | Contract |
|---|---|
| `test_step_input_tokens_include_cache_for_every_provider` | Anthropic step input = prompt + cache read + cache write; OpenAI = prompt; raw fields unchanged |
| `test_step_input_tokens_are_not_guessed_without_a_known_provider` | no usage records and no rollup → no `input_tokens` |
| `test_models_break_the_run_down_by_model` | per-model calls/tokens/cost sum to the run's usage records; largest first; routed and baseline carried |
| `test_model_input_tokens_match_the_bow_runs_convention` | breakdown input = the `bow.runs` "input includes cache" figure |
| `test_primary_model_is_the_rolled_up_planner_model` | the planner's model, not the larger codegen model; `None` for legacy runs |
| `test_tokens_and_models_follow_the_trace_permission` | scoped manager sees only runs on agents they manage; members refused |
| `test_queries_that_do_not_name_the_conversation_exclude_it` | broad, other-report, OR/NOT queries exclude it; refresh returns the same rows |
| `test_a_query_that_names_the_conversation_includes_it` | `report_id:<current>` (alone, in a list, with other terms) and `run_id:` include it; refresh returns the same rows |
| `test_field_predicates` (new cases) | `llm:` matches any model the run called (case-insensitive, lists, wildcards, `has:`, `NOT`); `routed:true/false` |

## Loop B — live (real stack, Claude Haiku 5.5)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --demo
ANTHROPIC_API_KEY=... HAIKU_MODEL_ID=claude-haiku-5-5 uv run python ../tools/agent/setup_haiku_llm.py
uv run python ../tools/agent/trace_tokens_live.py      # exits non-zero on any FAIL
```

`claude-haiku-5-5` is already a catalog preset (`backend/app/models/llm_model.py`,
1M context, 128K output, $0.10 / $0.50 per MTok, which matches Anthropic's
published model table), so no catalog change was needed.

Observed (abridged):

```
PASS multi-step run: 3 planner steps with tokens
PASS step e0abec54: in 77618 == prompt 3782 + cache 73836, out 139
PASS step 0b447294: in 79412 == prompt 1944 + cache 77468, out 397
PASS step 1b3a1401: in 81313 == prompt 1440 + cache 79873, out 177
PASS trace model claude-haiku-5-5 == agent_executions.primary_model_id claude-haiku-5-5
PASS models breakdown: [('claude-haiku-5-5', 7)]
PASS the agent named this conversation (report_id:<current>) in a bow.runs query
PASS the broad query did not name it
PASS broad query: 7 rows, none from this conversation
PASS report_id:<current>: [(2, 273, 16), (1, 232856, 721)]
PASS llm:claude-haiku-5-5 returns the chat and training runs (9 runs)
```

In the training conversation the agent wrote this query by itself, using the
new `current_report_id` line in the BOW client description:

```python
ds_clients["bow"].execute_query({"dataset": "runs", "query": "report_id:<this report>", ...})
```

The first live attempt failed in an instructive way: completions posted without
`"mode": "training"` in the prompt body ran as chat, so BOW was never offered.
Discovery follows the request mode, not the report's stored mode (as
`test_bow_discovery_uses_current_request_mode_and_manage_access` already pins).

**Known limit, seen live:** turn 2 shows 273 input tokens in the query above.
That query ran seconds after the turn, before the late usage records
(follow-ups) were rolled up. The DB settles at 231,072 for that turn. This is
the "turn still settling" limit the spec calls out
(`bow_source_service.py`, unindexed runs); it is not changed here.

UI captures (signed-in user, real UI): `tools/agent/trace_tokens_shots.mjs`
writes before/after shots to `media/pr/bow-216-trace-tokens-and-models/`.

## The fix

- `console_service.get_agent_execution_trace` loads the run's usage records once:
  - It sets `plan_decision.metrics_json.token_usage.input_tokens` on each block
    (cache included, by the planner's provider: the rollup's primary, else the
    planner-scope record; unset when unknown).
  - It returns `models[]`: per model, the calls, input/output/cache-read tokens,
    cost, routed flag and baseline.
- `AgentExecutionSchema` gains `primary_model_id` / `primary_provider`.
- `BowSourceService` keeps the exclusion unless a top-level term names the
  conversation: `report_id:<current>` drops it, and `run_id:<x>` admits those
  runs. `BowClient.description_for(report_id)` adds `current_report_id` for the
  coder and the planner's BOW schema section.
- The diagnosis registry gains `llm:` (EXISTS on `llm_usage_records.model_id`)
  and `routed:` in `fields.py` / `compiler.py`, mirrored in
  `frontend/utils/diagnosisQuery.ts`. `RunsTable` gets a Model column whose
  value pivots to `llm:<model>`.
- `TraceModal`:
  - in/out tokens per step: in the step-timing block (with cached) and on each
    timeline row;
  - the turn chip splits into in/out;
  - a model chip expands a per-model row and marks Auto-routed runs;
  - "Unknown model" when there are no usage records.

After the fix, the Loop A selection plus the neighbouring suites (BOW source,
token split, diagnosis scope, console scope, agent lists, MCP training, memory,
contract/grammar units): **373 passed** on `--db=sqlite`.

## Review fixes (PR review)

Each fix has a regression case that fails on the previous commit (13 failed, 28 passed with the fixes stashed):

- The BOW current-report check reads string values only for `report_id` / `run_id`. Before, `turn:1`, `eval:false`, `routed:false` or `tokens:(100 OR 200)` next to the exclusion raised `KeyError('s')`. Refresh is covered for each.
- `has:routed` means "the run has usage records" and no longer raises `IndexError`.
- Routed per model counts only calls that ran instead of the baseline: the router stamps every call of an Auto-routed run, escalated baseline calls included. The baseline is resolved from its `LLMModel` id to a model id. The run-level `auto_routed` flag drives the Auto badge.
- Step input is left unset when the run's planner calls follow different cache conventions (Auto routing across providers), because a decision can't be tied to its call.
- Before rollup, the trace's primary model comes from the usage records by the rollup's rule (planner calls first), not the largest model.

After the fixes, the same suites as above: **384 passed** on `--db=sqlite`.

## What this proves / regression notes

- Per-step numbers equal the DB's `plan_decisions.metrics_json` with one
  documented normalization, and the model shown equals
  `agent_executions.primary_model_id`.
- The self-observation default is intact. Only an explicit top-level name opts
  in, and since the query is the saved recipe, refresh is stable.
- **Out of scope:** LLM calls made *inside* tools (codegen) are not attributed
  to steps. `llm_usage_records` has no `tool_execution_id`, and adding one needs
  a migration. Step tokens are the planner call's; the turn chip and the models
  row cover every call.
- The turn chip and the models row count every call attributed to the run (the
  `bow.runs` convention). The conversation header leaves out background scopes
  (judge, titles, compaction), so the two can differ slightly (264k vs 259k
  live).
