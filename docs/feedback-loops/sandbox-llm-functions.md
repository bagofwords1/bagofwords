# Feedback Loop — "allow llm functions inside step.code (llm.classify etc.)"

Generated code (`create_data`'s `generate_df`) could query data, fetch pages and
drive a browser, but it could not ask an LLM anything. Labeling thousands of rows
meant the planner reading rows and labeling them itself, which is slow, costly
and capped by context. This loop adds an `llm` helper to generated code:
column-level functions in the style of BigQuery `AI.CLASSIFY` / Snowflake
`AI_CLASSIFY`. They run on the models the user may use, use the org's decision
model for classify/filter/score when there is one, and record every request as
LLM usage.

```python
def generate_df(ds_clients, excel_files, llm):
    names = ds_clients["Music Store:SQLite Chinook"].execute_query(
        "SELECT DISTINCT Name AS track_name FROM Track ORDER BY Name")
    labels = llm.classify(names["track_name"], ["love", "party", "sad", "angry", "other"])
    ...
```

The code above is what the live coder (Claude Haiku 5.5) wrote, unedited.

## Design (what was built)

| Piece | Where |
|---|---|
| `llm` stand-in in the sandbox: normalizes the input (Series / list / DataFrame rows / scalar), de-duplicates, makes **one RPC per call**, and shapes answers back onto the input index | `backend/app/ai/code_execution/sandbox/namespace.py` (`LLMHelper`) |
| RPC `llm` (child → trusted parent), same pattern as `browser` / `lists` | `sandbox/child.py`, `sandbox/runner.py`, `code_execution.py` (both sandbox modes) |
| Trusted engine: model catalog + access, answer cache, per-run cap, chat batching, decision fan-out, validation, usage | `backend/app/ai/code_execution/sandbox_llm.py` |
| Wiring for the agent run and **every re-run** of saved code (step rerun, query run / viewer run / preview, dashboard filter run) | `sandbox_helpers.py` (`LLMScope`, `llm_scope_for`), `services/{step,query,dashboard_runs}*.py` |
| Coder prompt lists each usable model with **both `model_id` and `record_id`** | `create_data.py` `sandbox_llm_directive` → `sandbox_llm.llm_directive_text` |
| Planner hint: route "label/score these rows" to one `create_data` call | `agent_v2.py`, `schemas/ai/planner.py`, `prompt_builder_v3.py` |
| Org settings `enable_sandbox_llm` (off by default), `sandbox_llm_max_values` (5000, clamp 1–50000) | `schemas/organization_settings_schema.py`, `frontend/pages/settings/ai_settings.vue`, `locales/{en,es,he}.json` |
| `LLM.ainference` (awaited, thread-offloaded) and a usage sink so a fan-out writes its usage rows **in one transaction** | `backend/app/ai/llm/llm.py` |

API in generated code:

```
llm.classify(values, labels, instructions=None, model=None, with_proba=False)
llm.filter(values, condition, model=None, threshold=0.5, with_proba=False)
llm.score(values, criterion, low=1, high=5, model=None)
llm.extract(values, fields, instructions=None, model=None)   # -> DataFrame
llm.generate(values, prompt, model=None)
llm.models()
```

Decisions:
- **`model=` takes the record id (`LLMModel.id`).** A provider `model_id` works
  only when exactly one enabled record has it. An ambiguous one is refused, and
  the refusal names the records to choose from. `model_id` alone is not unique
  in an org (two providers can serve the same id), and access grants, prices and
  decision settings are per record.
- **Default model per operation:** classify/filter/score go to the org's
  decision model (the Auto router's, else the first usable one) when it answers
  that question type; otherwise they use the user's small default chat model.
  extract/generate always use a chat model.
- **Access:** the agent's run checks `user_can_use_model` for every model.
  A re-run of saved code (refresh, a reader's run) runs the model the code
  names as long as the org still has it enabled, so a reader without access
  to that model still sees the dashboard. Code a user supplies on a re-run
  (`code_override`, query preview) is checked against that user.
- **Answers are cached** (process-wide LRU keyed by org, model record, question,
  value). Valid answers and deliberate "no value" answers (`null`, a decision
  refusal) are cached; provider errors and missing replies are not.

## Root cause of the first live failure (validated)

The first live run timed out: `create_data error 300067 ms` (the 300s hard
tool timeout), twice, and the backend logged
`Dropping LLM usage record after 4 lock retries: scope=sandbox_llm` 50 times.
Each chat batch awaited its own usage write. On SQLite the agent's session
holds the single writer while the tool runs, so every write sat through
0 + 0.5 + 2 + 5 s of retries and was then dropped. The requests also kept
running after the tool was abandoned, which is why a later "refresh" showed 9
"new" usage rows.

Fixes:
- `LLM.ainference(..., usage_sink=...)` and `LLM.decide(..., usage_sink=...)`
  collect rows. `SandboxLLM` writes **one batch per call** in a tracked
  background task (`LLM.write_usage_rows`, longer retry ladder), and
  `create_data` commits the agent's writer and then awaits `llm.drain()` before
  rolling up the tool's activity.
- `SandboxHelpers.end_attempt()` calls `llm.cancel()`: requests of an
  abandoned attempt stop.
- One `LLM` per concurrency slot (provider clients keep per-call state such as
  last usage).

The second live run surfaced the other issue: 262 / 3257 values (8%) came back
empty. The raw replies (instrumented) were explicit `null`s for titles the model
was unsure about, because the prompt said "null if none applies", and every
null was retried. Fix: the prompt says to always pick the closest label (null
only for empty/unreadable items), and an explicit null is a final, cached
**declined** answer, not a failure to retry.

Throughput tuning, measured on Claude Haiku 5.5 with the real prompt:

| Reply shape / batch | Items | Seconds | Output tokens |
|---|---|---|---|
| `{"results":[{"i":0,"answer":..}]}` / 50 | 50 | 6.0 | 1608 |
| `{"answers":{"0":..}}` / 50 | 50 | 4.9 | 1100 |
| `{"answers":{"0":..}}` / 100 | 100 | 6.9 | 2137 |

→ batches of 100, 16 in flight.

## Loop A — deterministic (no external services)

Provider boundaries faked (`tests/e2e/rbac/test_sandbox_llm.py`: a fake
Anthropic client that answers the items it was sent, and
`tests/mocks/fake_decision_server.py`). Orgs, models, groups and grants are
seeded through the API, and the sandbox child and RPC run for real.

```bash
cd backend
export BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true
uv run pytest tests/e2e/rbac/test_sandbox_llm.py tests/unit/test_sandbox_llm_shapes.py \
  tests/unit/test_sandbox_helpers.py -q -n 3
# 74 passed
```

What the tests pin (each was broken on purpose and seen to fail: dedupe,
access check, cache, label validation, decision routing, value cap):

- one valid label per row, aligned to the index; an off-list answer → None
- 2100 rows / 7 distinct → 1 request carrying each distinct value once
- `2*batch+37` distinct values → `ceil(n/batch)` requests, all answered
- a refresh with values answered before → 0 requests, same labels; a different
  question → new requests
- model by record id, by unique model id; an ambiguous id is refused naming
  both records; an unknown id lists the usable ones
- restricted model: works for the granted group member, refused (before any
  request) for another member
- coder prompt: lists `model_id` and `record_id` of usable models only
- decision model: classify (`with_proba`), filter and score go to it, one
  request per distinct value; naming a chat model overrides; extract/generate
  stay on chat
- usage: one row per provider request, scope `sandbox_llm`, org/user/report,
  `scope_ref_id` = step, `llm_model_id` = the record
- per-run cap stops before any request; setting off → `HelperUnavailableError`
- missing items retried once (only those); explicit null final and cached;
  provider error not cached

## Loop B — live (Claude Haiku 5.5, real agent)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --demo
ANTHROPIC_API_KEY=... HAIKU_MODEL_ID=claude-haiku-5-5 uv run python ../tools/agent/setup_haiku_llm.py
uv run python ../tools/agent/sandbox_llm_live.py --db-path db/agent.db
```

Prompt: *"Using the LLM functions in code, classify every distinct track name
in the Track table by mood (love, party, sad, angry, other) and give me a table
of mood with the number of tracks in each."*

| | Before fixes | After |
|---|---|---|
| Agent run (HTTP) | 615.8 s, `create_data` hit 300 s timeout ×2, no step | **24.9 s**, 1 step |
| `create_data` | timeout | 20.6 s (llm part 16.0 s) |
| Values / requests | — | 3257 / **33** (= ⌈3257/100⌉) |
| No-answer values | 262 (8%) in the second run | **0** |
| Usage rows dropped | 50 | **0** (33 written, attributed to org/user/report/record) |
| Refresh | 9 stray rows | **0.6 s, 0 new requests** (cache) |
| Cost | — | $0.018 |

Result: other 1531, sad 574, love 430, angry 385, party 337 (sum 3257).

`sandbox_activity.llm` in the tool result (what the planner sees):

```json
{"calls": 1, "ops": {"classify": 1}, "rows": 3257, "unique_values": 3257,
 "values_sent": 3257, "cache_hits": 0, "requests": 33, "failed_values": 0,
 "declined_values": 0, "usage_rows_dropped": 0,
 "models": {"<record id>": {"model_id": "claude-haiku-5-5", "kind": "chat",
            "requests": 33, "values_sent": 3257}}, "elapsed_ms": 15975.3}
```

UI evidence (AI Settings → Sandbox): `media/pr/ai-eager-darwin-xfkm5n/`
`before-sandbox-settings.png`, `after-sandbox-settings.png`,
`after-sandbox-settings-he.png` (RTL).

## Known limits / follow-ups

- The answer cache is in-process memory: a backend restart, or another worker,
  asks again (and pays). A persistent cache table would make refreshes free
  across workers.
- Usage rows from the agent's own run have no `scope_ref_id` (the step does not
  exist yet when the code runs). They carry `agent_execution_id`, so they still
  roll up to the run. Re-runs carry the step id.
- Re-runs of saved code do not re-check the viewer's access to the model (see
  Decisions). That is intentional, and worth stating in the admin docs.
- Semantic joins and `llm.agg` (grouped summaries) are not in v1.
- In this sandbox the 8-worker unit run plus the stack ran out of memory, and a
  restart without a pinned `BOW_ENCRYPTION_KEY` made stored credentials
  unreadable. Both are environment issues, not product issues.
