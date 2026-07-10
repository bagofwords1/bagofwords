# Concurrent multi-tool execution — investigation & map

**Status: investigation only. No implementation yet.**

Goal scenario: the planner emits N independent tool calls in one decision —
e.g. `create_data` / `inspect_data` across 5 different data sources — and the
agent dispatches them concurrently (`asyncio.gather` + concurrency cap)
instead of the serial `for` loop, cutting wall-clock roughly to the slowest
action instead of the sum.

This doc maps what already exists, what blocks concurrency (ranked), a design
sketch, and the sandbox feedback loop (per `.agents/skills/sandbox-feedback-loop`)
that the implementation should be validated with.

---

## 1. What already exists (multi-action plumbing)

The multi-action path is fully built end-to-end — it is just *dispatched
serially* and *rarely triggered* because providers are asked not to emit
parallel tool calls.

| Piece | Where | Notes |
|---|---|---|
| Planner collects **all** `tool_use` blocks | `backend/app/ai/agents/planner/planner_v3.py:135-136, 179-189, 258-279` | `ToolUseStart/CompleteEvent` paired by provider `tool_use_id`; placeholder replaced on complete. |
| Decision carries the list | `backend/app/schemas/ai/planner.py:44-63` | `PlannerDecision.actions: List[Action]`; `action` kept as legacy first-action. `Action` = `{type, name, arguments}` — **no `id` field**; the provider `tool_use_id` is dropped after pairing. |
| Serial dispatch loop | `backend/app/ai/agent_v2.py:2788-2790` (list build), `2868-2903` (per-action blocks), `2902` (`for tool_index, action in enumerate(actions_list)`), `3503` (break) | One block + one `tool_execution` row per action. |
| Multi-block per decision | `backend/app/project_manager.py:1426, 1454, 1470-1471` | `upsert_block_for_decision(..., force_insert=True, tool_index=i)`; `block_index = seq*100 + tool_index` gives 100 sub-slots per decision. |
| Extra blocks pre-created + emitted | `agent_v2.py:2876-2901` | Stable block id per action before any tool runs. |
| Per-action persistence | `agent_v2.py:3313-3401` | `_bg_persist_tool(_block_id=...)` binds the per-action block id eagerly. |

### Feedback to the planner is textual, not id-paired

The next planner iteration receives results as synthesized text — a single
user message containing `<past_observations>` + `<last_observation>` JSON
(`backend/app/ai/agents/planner/prompt_builder_v3.py:62-71, 501-504`). There is
**no** assistant `tool_use` / `tool_result` transcript, so there is no
protocol-level requirement to return results in call order or pair them by id.
That is convenient for concurrency: completion order does not matter to the
model, only that every action's observation lands in `past_observations`.

Caveats in the feedback path:

- `last_observation` is a **single dict**; in the serial loop the `observation`
  variable is overwritten per action, so the planner's `last_observation` today
  is whichever action ran **last** (`agent_v2.py:2260`, assignment inside the
  per-action loop at `3069-3076`). With N concurrent actions this must become
  an aggregate (list or merged summary).
- `save_plan_decision_from_model` persists only the **first** action's
  name/args onto the `PlanDecision` row (`project_manager.py:1213-1234`);
  the `actions` list itself is not persisted (blocks/tool_executions are the
  per-action record).

### Why the list is almost always length 1 today

Parallel tool calls are disabled per provider:

| Provider | Where | Honored? |
|---|---|---|
| Anthropic | `backend/app/ai/llm/clients/anthropic_client.py:328-331` (`disable_parallel_tool_use: True`) | Yes. Debug override: `BOW_FORCE_PARALLEL_TOOLS=1` (`:325-327`). |
| OpenAI / Azure | `openai_client.py:266-267`, `azure_client.py:209-210` (`parallel_tool_calls=False`) | Yes. |
| OpenAI Responses | `openai_responses_client.py:232-233` | Yes (function tools only). |
| Bedrock | `bedrock_client.py:252, 266-270` | **No** — param accepted, `disableParallelToolUse` deliberately not sent (older botocore compat). |
| Google/Gemini | `google_client.py:213` | **No** — param declared, never used. |

So to *trigger* the 5-source scenario deliberately, the flag must be flipped
(Anthropic already has the env override; OpenAI/Azure would need the same) and
the planner prompt likely needs to *invite* parallel calls for independent
sources. Bedrock/Gemini can already emit multi-action decisions today — which
is why the serial fan-out loop exists.

---

## 2. Blockers for `gather`-based dispatch (ranked)

### B1 — Global stdout lock serializes the entire code-exec + query window

`backend/app/ai/code_execution/code_execution.py:30` defines a module-level
`_STDOUT_REDIRECT_LOCK`, acquired at `:694-713` around **`exec(code)` AND
`generate_df()`** — i.e. around the actual data-source queries, since
`generate_df` is where the wrapped clients run SQL. The rationale comment
(`:22-29`) argues user code is "CPU-bound by the GIL, so wall-clock impact is
negligible" — true for pandas, **false for I/O-bound warehouse queries**, which
release the GIL during network waits.

Consequence: even with a perfect `gather` in agent_v2, five concurrent
`create_data` executions would parallelize **only codegen** (the LLM call,
which is async and outside the lock) and then queue single-file on this lock
for the query/execution phase. This lock must be rescoped before the headline
scenario pays off — e.g. install a process-wide `sys.stdout` proxy **once**
that dispatches writes to a per-thread (`threading.local`) buffer, so no global
mutation happens per execution and the lock disappears entirely. Note the
per-query timeout already runs each query in its own daemon thread
(`code_execution.py:451-481`), so stdout from query threads is a pre-existing
edge case either way.

Related: `_CODE_EXEC_POOL` is `min(8, cpu*2)` workers (`:67-70`) — fine as a
floor for a concurrency cap of 3–5, but it is shared process-wide across all
concurrent completions, so the agent-level cap should stay well below it.

### B2 — Shared `AsyncSession` (`self.db`) + single-writer architecture

- `runtime_ctx["db"] = self.db` (`agent_v2.py:2996`) — the agent's single
  long-lived session, shared across the whole loop. SQLAlchemy `AsyncSession`
  is **not** safe for concurrent use; two coroutines awaiting on it
  concurrently raise (`InvalidRequestError` / greenlet errors).
- In single-writer mode — **always on for SQLite**, opt-in on Postgres via
  `BOW_AGENT_SINGLE_WRITE_SESSION` (`agent_v2.py:1685-1703`) — `self._writes =
  self.db` (`:1894-1895`) and *all* migrated writers (`_handle_tool_output`
  at `:4467`, `_handle_streaming_event`'s step/query creation, persist-tool at
  `:3369-3399`, transcript rebuilds at `:1838`) funnel through that one session
  *by design* (SQLite allows one write txn).
- The per-action body is full of `self.db` touches: `next_seq` calls,
  `start_tool_execution_from_models` (`:2938`), context refreshes, block
  upserts.

Consequence: DB work cannot simply run inside gathered coroutines. Options:
(a) keep **DB writes serialized** behind an `asyncio.Lock` (or a small
write-queue/actor) while only the *tool execution itself* (LLM codegen + code
exec) runs concurrently; (b) per-action short-lived sessions on Postgres
(legacy mode already does this pattern in `_bg_persist_tool`), keeping the
single-writer funnel on SQLite. Option (a) preserves the single-writer
invariant on both backends and is the least invasive.

Also: `_release_db_between_steps` (`:1865-1880`) commits `self.db` before each
tool run; with gather it should run once before the batch, not per action.

### B3 — Orchestrator per-tool mutable state on `self`

`self.current_query / current_step / current_step_id / current_visualization /
current_widget` are fields on the agent instance:

- Reset before each `create_widget`/`create_data`/`describe_entity` action
  (`agent_v2.py:2923-2935`).
- **Written by `_handle_streaming_event`** in response to `tool.progress`
  events — e.g. `data_model_type_determined` creates Query+Step+Visualization
  and sets all four (`agent_v2.py:4183-4216`).
- Read after the tool finishes to attribute created objects
  (`created_step_id` fallback at `:3190-3191`, `created_visualization_ids` at
  `:3203-3205`) and read by tools via `runtime_ctx` (`create_data.py:1715,
  1834, 1891`).

Two concurrent `create_data` runs would clobber each other's
`current_step_id`, mis-attributing steps/visualizations to the wrong block.
Fix shape: make this state **per-invocation** — key it by `tool_execution.id`
(already passed into `runtime_ctx["tool_call_id"]` at `:3023`) and route
`_handle_streaming_event` by the emitting invocation instead of `self.*`.
This is the largest refactor item.

### B4 — SSE streaming events carry no per-tool identity; frontend assumes one running tool

- Backend: the `emit` wrapper forwards `tool.progress|stdout|partial|error|
  confirmation` with only `{tool_name, payload}` — **no block_id /
  tool_execution_id** (`agent_v2.py:3033-3048`). Same for `tool.started`
  (`:2963-2972`).
- Frontend (`frontend/pages/reports/[id]/index.vue`): `tool.started` (~line
  2148), `tool.progress` (~2222), `tool.stdout` (~2451), `tool.partial`
  (~2477) all write onto **`lastBlock` = last completion block**. Only
  `tool.finished` (~2532) targets by `block_id`/`tool_execution_id` (with
  lastBlock fallback), and `block.upsert` / `block.delta.artifact` are
  id-keyed.

Consequence: concurrent streams would interleave onto one block in the UI.
Fix shape: attach `block_id` + `tool_execution_id` to every tool.* SSE event
(backend already knows both at emit time) and switch the frontend handlers to
id-based routing with the lastBlock fallback retained for old events.

### B5 — `ToolRunner` shared mutable state

One `ToolRunner` per agent (`agent_v2.py:405-408`) with a cross-call
`validation_failure_count` (`tool_runner.py:25-26, 48, 54, 148`) that
increments on validation failure and **resets to 0 on any success** —
interleaved concurrent runs corrupt the count. Fix: per-invocation counter
(keyed by tool name) or instantiate a runner per action; the rest of
`run()` is per-call local (timeout tasks, retry loop) and gather-safe.

### B6 — Loop-level aggregation & circuit breakers

All currently updated inside the per-action loop; with gather they need a
post-`gather` aggregation pass over `(action, observation)` pairs:

- `observation` last-wins → becomes aggregate for `last_observation` (B-planner
  note above); each action still calls
  `observation_builder.add_tool_observation` (`agent_v2.py:3481-3487`) — that
  builder is an append-only list, event-loop-safe.
- `analysis_done` / `analysis_complete` + `final_answer` handling
  (`:3122-3180`): decide policy — e.g. any action with `analysis_complete=True`
  finishes the turn *after* all gathered actions complete; `completion.finished`
  must be emitted once (`completion_finished_emitted` guard already exists).
- Circuit breakers: `failed_tool_count` (`:3082-3089`),
  `successful_tool_actions` repeat detector (`:3093-3102`), artifact-call
  counters (`:3105-3120`) — evaluate on aggregated results, not per-coroutine.

### B7 — Data-source clients

`self.clients` → `runtime_ctx["ds_clients"]` (`agent_v2.py:3015`): one client
instance per connection per run (`data_source_service.py:2132-2210`), shared
by reference. Per-invocation `QueryCapturingClientWrapper`s are fresh
(`code_execution.py:563, 1166-1167`), so capture lists don't collide.

- **Different sources concurrently** (the target scenario): different client
  objects — safe.
- **Same source concurrently**: same underlying client object; thread-safety
  is client-class dependent (each query already runs in its own daemon
  thread today, so same-source concurrency exists *within* one execution —
  but not across executions). Reasonable v1 policy: cap per-data-source
  concurrency at 1, i.e. only parallelize across distinct sources.

### Non-blockers (verified)

- `project_manager.next_seq` (`project_manager.py:1413-1420`) is async but has
  **no await points** — under cooperative asyncio scheduling the
  read-modify-write cannot interleave, so gathered coroutines can call it
  safely (not thread-safe, but SSE emission stays on the event loop).
- SSE ordering: events are seq-numbered and the frontend already handles
  id-keyed `block.upsert` out of order.
- `inspect_data` is not in `_DATA_TOOLS` (`agent_v2.py:4151-4161`), so it does
  no DB writes via streaming events at all — it is the *easiest* tool to run
  concurrently (reads schema context, generates code, executes; only B1/B5
  apply).

---

## 3. Design sketch (for the implementation PR)

1. **Extract the per-action body** (`agent_v2.py:2902-3499`) into an
   `async def _run_single_action(action, tool_index, block_id, decision, ...)`
   returning `(observation, tool_output, tool_execution, created_ids...)`.
   This is 90% mechanical; the `action`/`observation`/`tool_name` locals
   become parameters/returns instead of loop-shared variables.
2. **Dispatch**: `asyncio.Semaphore(cap)` wrapping each action;
   `results = await asyncio.gather(*coros, return_exceptions=True)`.
   Two distinct limits (decided):
   - **Accept-cap**: at most **10** actions honored per decision (constant).
     Beyond 10, truncate and tell the planner via the observation
     ("N more actions were not executed; re-issue them") — never silently.
   - **In-flight cap**: an **org setting** `ai_tool_concurrency`
     (`FeatureConfig` in `organization_settings_schema.py`, read via
     `organization_settings.get_config("ai_tool_concurrency").value` like
     `limit_row_count`), default **1** = today's behavior, recommended max 4–5
     (must stay well under `_CODE_EXEC_POOL`'s 8 shared workers).
   Group actions by data source and keep per-source concurrency at 1 (B7).
3. **DB discipline**: keep one writer. Wrap every DB-touching section inside
   `_run_single_action` with a shared `asyncio.Lock` (or route through the
   existing `_writes_session` plus a lock), so only the awaits on LLM codegen
   and `execute_code_async` actually overlap. This respects the single-writer
   invariant on SQLite unchanged.
4. **Per-invocation tool state** (B3): replace `self.current_*` with a
   `dict[tool_execution_id -> ToolInvocationState]`;
   `_handle_streaming_event` gets the invocation id from the emit closure
   (each action builds its own `emit` capturing its `tool_execution.id`).
5. **SSE identity** (B4): add `block_id` + `tool_execution_id` to
   `tool.started/progress/stdout/partial/error`; update the frontend handlers
   to id-routing with lastBlock fallback.
6. **Observation aggregation & context policy** (B6, decided):
   - `last_observation` becomes a compact aggregate when N>1:
     `{"parallel_actions": [{tool_name, data_source, summary, step_id/query_id,
     error}, ...]}` — summaries + referenceable ids, not N full payloads.
   - Observations are recorded in **tool_index order**, not completion order —
     deterministic prompts (cache-friendly, reproducible tests).
   - `_compact_past_observations` keeps the last `_RECENT_OBS_FULL = 5`
     observations full (`prompt_builder.py:17,518-544`) — a count-based window
     that would minify half of a 10-action batch before the planner sees it.
     Make it **iteration-aware**: keep the last iteration's whole batch full,
     minify prior iterations.
   - Per-observation size budget for batch members (inspect_data observations
     with df samples run 1–4k tokens each; 10×/iteration compounds fast);
     `trim_context_to_budget` stays as the backstop, with a scale test
     asserting planner input fits the model window with 10 fat observations.
   - `messages_context` is structurally unaffected (one system completion per
     turn; planner receives a single synthesized user message).
7. **Rescope `_STDOUT_REDIRECT_LOCK`** (B1): per-thread stdout proxy installed
   once at import; delete the lock (or shrink it to the proxy install). This
   is independently valuable — it currently serializes code exec across
   *concurrent completions*, not just multi-tool.
8. **Enable emission** where wanted: extend the `BOW_FORCE_PARALLEL_TOOLS`
   override to OpenAI/Azure, and/or a planner-prompt hint that independent
   per-source reads may be issued as parallel tool calls.
9. **`ToolRunner`** (B5): move `validation_failure_count` into per-call state.

Sequencing note: items 1–3 + 6 + 9 give correct-but-modest concurrency
(parallel codegen LLM calls only). Item 7 unlocks parallel query execution
(the actual 5-source win). Items 4–5 are required before enabling cap > 1 for
step-creating tools (`create_data`); `inspect_data`-only concurrency could
ship without them.

---

## 4. Sandbox feedback loop plan (`.agents/skills/sandbox-feedback-loop`)

Loop A (deterministic, no external services — SQLite + stubbed LLM/clients):

1. **Repro/baseline test** (pytest, `backend/tests/`): drive the agent loop
   with a stubbed planner that returns one decision whose `actions` contains
   5 `inspect_data`/`create_data` calls against 5 fake data sources, each
   stub tool sleeping ~0.5s. Assert today: total wall-clock ≈ 5×0.5s
   (serial), 5 tool_execution rows, 5 blocks with distinct `block_index`
   sub-slots, and `last_observation` == 5th action's observation.
2. **Invariant assertions the fix must keep**: one `tool_execution` +
   one block per action with correct attribution
   (`created_step_id`/`created_visualization_ids` per block, not
   cross-attributed); every observation present in
   `observation_builder.tool_observations`; `completion.finished` emitted
   exactly once; single-writer mode (SQLite) produces no "database is locked".
3. **Post-fix flip**: same test with cap=5 asserts wall-clock ≈ 1×0.5s + ε,
   and the attribution/ordering invariants above still hold. A second test
   with cap=1 asserts behavior identical to baseline (rollback safety).
4. **Stdout-lock leg**: two concurrent `execute_code_async` calls whose code
   `print()`s and sleeps in `generate_df` (via a stubbed client with a
   `time.sleep` inside `execute_query`); assert overlap post-rescope and
   correct, non-interleaved captured stdout per execution.
5. **UI leg** (only when B4/B5 land): per `ui-evidence` skill, before/after
   showing two tool cards streaming progress simultaneously without
   clobbering.

Live confirmation (Loop B, optional): `BOW_FORCE_PARALLEL_TOOLS=1` with an
Anthropic key on a report with ≥2 sources; prompt "inspect X in all sources".

Doc goes to `docs/feedback-loops/concurrent-multi-tool-execution.md` when the
implementation lands, with observed FAIL→PASS output per the skill template.

---

## 5. Implementation plan — phased, feedback-loop-driven

Each phase is independently shippable, gated by its own verify loop, and
ordered so concurrency is opt-in (default cap 1 = today's behavior) until the
evidence says otherwise. Build/verify/iterate happens against the **real
stack in the sandbox**, per `.agents/skills/sandbox-feedback-loop`.

### The real-env harness (used by every phase)

```bash
# Fresh sandbox setup (Python 3.12, per the skill)
cd backend && pip install uv && uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" && mkdir -p db

# Full stack: backend :8000 + frontend :3000, SQLite at backend/db/agent.db
tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py --demo
```

- **Five data sources**: extend `tools/agent/seed_org.py` (or add
  `tools/agent/seed_multi_ds.py`) to create N SQLite data sources through the
  real API (`sqlite_client.py` exists in
  `backend/app/data_sources/clients/`), each pointing at its own seeded
  `.db` file with a distinct table (`orders_1`..`orders_5`). Everything
  in-sandbox, no external warehouse needed.
- **LLM**: real provider through the API, exactly as the e2e suite does
  (`backend/tests/fixtures/llm.py` — key from an env var like
  `OPENAI_API_KEY_TEST`; secrets via env only, never committed/echoed).
  `BOW_FORCE_PARALLEL_TOOLS=1` (Anthropic) to invite multi-action decisions.
- **Drive**: create a report attached to all 5 sources via the API, POST a
  completion ("inspect the orders table in each of the five sources"),
  stream SSE.
- **Measure**: `tool_executions.started_at/duration_ms` rows give the
  timeline (overlap vs serial); the `code_execution.stdout_lock` span
  attribute `lock_wait_ms` (`code_execution.py:697-698`) quantifies
  stdout-lock queueing directly.
- **UI evidence** (Phase 4): `tools/agent/capture.mjs` + the `ui-evidence`
  skill for before/after of concurrent tool cards.

Deterministic legs live in `backend/tests/` (SQLite default, autouse
migrations, LLM/planner stubbed) so they run in CI without credentials; the
real-env loop is the iterate/perfect surface, and its transcript becomes
Loop B in the feedback-loop doc.

### Phase 0 — Harness + baseline (no product change)

- Multi-source seeding helper (above).
- Loop A baseline test: stubbed planner emitting one decision with 5 actions,
  stub tools sleeping ~0.5s → assert **serial** wall-clock (≈2.5s), 5
  tool_execution rows, 5 blocks with distinct `block_index` sub-slots,
  `last_observation` == 5th action's observation.
- Real-env baseline: run the 5-source prompt, record the serial
  tool_executions timeline + `lock_wait_ms`.
- Exit: `docs/feedback-loops/concurrent-multi-tool-execution.md` started with
  the observed FAIL(serial) evidence.

### Phase 1 — Mechanical extraction (behavior-neutral)

- Extract the per-action body (`agent_v2.py:2902-3499`) into
  `_run_single_action(...)` returning an outcome object
  (observation, tool_output, tool_execution, created ids); loop stays serial.
  The loop-shared `action`/`observation`/`tool_name` locals become
  parameters/returns.
- Fix B5: `ToolRunner.validation_failure_count` → per-call/per-tool state.
- Exit: full suite green; Phase-0 Loop A output byte-for-byte comparable
  (same timings ±ε, same rows/blocks); one real-env smoke run unchanged.

### Phase 2 — Concurrent dispatch core (opt-in)

- `asyncio.Semaphore` + `gather(return_exceptions=True)`; in-flight cap from
  the org setting `ai_tool_concurrency` (default **1**); accept-cap 10
  actions/decision with truncation surfaced to the planner.
- Group actions by `data_source_id` from `tables_by_source`; per-source
  concurrency stays 1 (B7).
- Shared `asyncio.Lock` around DB-touching sections inside
  `_run_single_action` — preserves the single-writer invariant (B2);
  `_release_db_between_steps` runs once before the batch.
- Aggregation (B6): `last_observation` becomes
  `{"parallel_actions": [...]}` when N>1; circuit breakers +
  `analysis_complete`/`final_answer` policy evaluated post-gather;
  `completion.finished` emitted once.
- Scope guard: concurrency only for tools **not** in `_DATA_TOOLS`
  (`agent_v2.py:4151-4161`) — i.e. `inspect_data`-class tools; step-creating
  tools still serialize until Phase 4.
- Loop A: cap=5 → wall-clock ≈ max not sum; cap=1 → parity with Phase 0;
  SQLite leg shows zero "database is locked"; attribution invariants hold.
- Real-env: cap=5 + `BOW_FORCE_PARALLEL_TOOLS=1` → codegen LLM calls overlap;
  exec window still queues on the stdout lock — record the interim timeline
  and `lock_wait_ms` as evidence for Phase 3.

### Phase 3 — Unlock parallel execution (stdout-lock rescope, B1)

- Install a process-wide `sys.stdout` proxy **once** that dispatches writes
  to a per-thread buffer; delete `_STDOUT_REDIRECT_LOCK` (or shrink it to
  proxy install). Keep the `lock_wait_ms`-style telemetry.
- Loop A: two concurrent `execute_code_async` calls whose `generate_df`
  prints and sleeps (stub client with `time.sleep` in `execute_query`) →
  assert wall-clock overlap AND correct, non-interleaved captured stdout per
  execution.
- Real-env: 5-source run now shows true overlap in the exec/query phase;
  `lock_wait_ms` ≈ 0. This phase also de-serializes code exec across
  concurrent completions — verify no stdout cross-talk under two parallel
  reports.

### Phase 4 — Streaming identity + per-invocation state (enables `create_data`)

- B4: attach `block_id` + `tool_execution_id` to
  `tool.started/progress/stdout/partial/error` SSE events
  (`agent_v2.py:2963-2972, 3033-3048`); frontend
  (`frontend/pages/reports/[id]/index.vue` handlers ~2148/2222/2451/2477)
  routes by id with lastBlock fallback for old payloads.
- B3: replace `self.current_query/step/step_id/visualization/widget` with
  per-invocation state keyed by `tool_call_id`
  (already in `runtime_ctx`, `agent_v2.py:3023`); `_handle_streaming_event`
  resolves the invocation from the emitting action's closure.
- Then admit `create_data`/`_DATA_TOOLS` to the concurrent set.
- Loop A: two concurrent stub `create_data` runs → steps/visualizations
  attributed to the correct blocks (no cross-attribution).
- Real-env + ui-evidence: 5-source `create_data` prompt; screenshot two tool
  cards streaming progress simultaneously without clobbering; verify each
  widget/step lands under its own block. Playwright spec via `capture.mjs`.

### Phase 5 — Emission + rollout

- Extend the parallel-tools override beyond Anthropic (OpenAI/Azure flag or
  org setting); planner prompt hint that independent per-source reads may be
  issued as parallel tool calls.
- Pick the shipped default cap (proposal: 3; hard ceiling well under
  `_CODE_EXEC_POOL`'s 8 workers), telemetry for concurrency level and lock
  waits, changelog + docs.
- Finalize `docs/feedback-loops/concurrent-multi-tool-execution.md` with the
  full FAIL→PASS evidence (Loop A outputs + real-env timelines).

### Standing rules for every phase

- Default-off: until Phase 5, cap defaults to 1 and every behavior change is
  env-gated; a cap=1 parity test is part of each phase's exit criteria.
- Every reproduction test survives as a regression test
  (`backend/tests/AGENTS.md`: assert the invariant, not the magic scenario).
- Real-env evidence (timelines, span attrs, screenshots) goes into the
  feedback-loop doc as each phase lands — the doc stays runnable.
