# Feedback Loop — "Concurrent multi-tool execution: swap the serial dispatch loop for gather + concurrency cap"

The planner can emit several tool calls in one decision (e.g. `inspect_data` /
`create_data` against 5 different data sources), but agent_v2 dispatched them
one at a time — wall-clock was the *sum* of the tool durations instead of the
max. This loop validates the concurrent dispatch path end-to-end: bounded
overlap, per-source serialization, per-invocation attribution, id-keyed UI
streaming, and the stdout-lock removal that makes overlapping code execution
real.

## Root cause (validated)

- Serial for-loop over `decision.actions` — `backend/app/ai/agent_v2.py`
  (formerly `for tool_index, action in enumerate(actions_list)`), now the
  closure + `_dispatch_action_batch` harness.
- `_STDOUT_REDIRECT_LOCK` in `backend/app/ai/code_execution/code_execution.py`
  was held around `exec(code)` **and** `generate_df()` — i.e. around the
  I/O-bound warehouse queries — serializing every code execution in the
  process (its comment claimed GIL-bound ≈ free; queries release the GIL).
- Shared per-tool state made naive gather unsafe: `self.current_query/step/…`
  mutated by `_handle_streaming_event`, one shared `AsyncSession` (single-writer
  mode), `ToolRunner.validation_failure_count`, last-write-wins `observation`,
  and `tool.progress/stdout/partial` SSE events carrying no block identity
  (frontend routed them all to the LAST block).

Found along the way (pre-existing, fixed here because it dominated the probe):
- `consume_data_query_with_context` / `consume_data_bytes_with_context`
  (`backend/app/services/usage_policy_service.py`) had no
  `has_feature("usage_limits")` gate — unlike `add_tokens` — so every sandbox
  query paid a quota bookkeeping write even in unlicensed installs. On SQLite,
  with a writer holding the lock during code execution, each write burned the
  full 30s busy_timeout: **60s floor per `inspect_data`/`create_data` call**
  (observed `query_ms: 30066` for a 4-row local SQLite GROUP BY).

## Loop A — deterministic reproduction (no external services)

```bash
cd backend && pip install uv && uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" && mkdir -p db
TESTING=true uv run pytest tests/unit/test_concurrent_tool_dispatch.py -q
```

33 tests covering: serial default; overlap bounded by
`BOW_AGENT_TOOL_CONCURRENCY` at 2/4/8 with wall-clock < 0.8× serial;
action-order preservation when later actions finish first; unsafe-tool batch
forced serial; same-source serialization with distinct-source overlap; crash →
error outcome; sigkill (serial + concurrent); serial invocation-state
chaining; a 20-iterations × 10-actions scale run (200 executions, order +
block attribution asserted); aggregation parity/partial-failure/final-answer/
image/`not_executed` semantics; per-invocation state reset+adoption; env cap
clamps; per-tool ToolRunner validation streaks; same-iteration observation
compaction exemption; iteration-aware prompt compaction window; stdout-router
no-cross-talk under 4 threads × 200 lines; and two `execute_code_async` runs
with 0.4s I/O-bound queries finishing in < 0.7s wall (previously ~0.8s+,
serialized by the global lock).

Observed on this branch: `33 passed`.
Pre-change behavior (validated during development): the dispatch defaulted
serial everywhere (`max_in_flight == 1` regardless of env) and the two
0.4s executions took ~2× wall under `_STDOUT_REDIRECT_LOCK`.

Full unit suite: `923 passed, 17 failed` on this branch vs
`890 passed, 17 failed` on the pristine base commit in a clean worktree —
identical failures (whatsapp adapter/webhook, permissions registry, …),
i.e. all 17 are pre-existing; the +33 passes are this branch's new suite.

## Loop B — full stack, real HTTP/SSE, deterministic stub LLM

The stub plays a provider that emits **parallel tool_use blocks** (as Bedrock
and Gemini already do in production — they ignore the disable flag), so the
whole path from planner streaming through dispatch, persistence, SSE, and the
UI runs for real with zero API keys:

```bash
tools/agent/boot_stack.sh --dev
cd backend
uv run python ../tools/agent/seed_org.py --sqlite-sources 5

# stub LLM (OpenAI-compatible, scripted: 5x inspect_data -> 5x create_data -> final)
STUB_SOURCES_FILE=/tmp/bow-agent/stub_sources.json STUB_PORT=9099 \
  uv run python ../tools/agent/stub_llm.py &   # sources JSON printed by seed_org

# FAIL leg (baseline): backend without the flag -> serial
uv run python ../tools/agent/run_concurrency_probe.py --stub --iterations 1

# PASS leg: restart backend with BOW_AGENT_TOOL_CONCURRENCY=5 -> overlap
BOW_AGENT_TOOL_CONCURRENCY=5 <restart backend>  # boot_stack env or systemd
uv run python ../tools/agent/run_concurrency_probe.py --stub --iterations 3
```

The probe measures overlap from `tool_executions.started_at/duration_ms`
(max concurrent depth + wall vs sum). Evidence recorded below.

### Observed — serial baseline (flag unset)

5× `inspect_data` dispatched from ONE planner decision, executed strictly
serially, ~60s apart (each stalled 2×30s in the ungated quota writes):

```
inspect_data  start=20:42:39  dur=60459ms   # orders_1
inspect_data  start=20:43:39  dur=60533ms   # orders_2
inspect_data  start=20:44:40  dur=60506ms   # orders_3
inspect_data  start=20:45:40  dur=60577ms   # orders_4
inspect_data  start=20:46:41  dur=60470ms   # orders_5
max_overlap=1 (serial), wall ≈ sum
```

### Observed — concurrent (BOW_AGENT_TOOL_CONCURRENCY=5)

Same stack, same stub, 3 iterations. Each planner decision's 5 tool calls
started within **~155 ms** of each other (vs 60s apart serially) and ran
overlapped; both phases (5× inspect_data, then 5× create_data) per iteration:

```
[agent] dispatching 5 tool calls concurrently (cap=5): inspect_data x5
inspect_data  start=22:15:09.817  dur=61764ms
inspect_data  start=22:15:09.859  dur=61858ms
inspect_data  start=22:15:09.897  dur=61387ms
inspect_data  start=22:15:09.934  dur=61499ms
inspect_data  start=22:15:09.971  dur=61114ms

iteration 0: tools=10  max_overlap=5  wall=129.1s  sum=631.9s   (4.9x)
iteration 1: tools=10  max_overlap=5  wall=134.0s  sum=645.3s   (4.8x)
iteration 2: tools=10  max_overlap=5  wall=139.9s  sum=664.2s   (4.7x)
SUMMARY: 3 completions, 3 with overlapping tool executions (max depth 5)
```

All 15 create_data runs materialized their steps correctly — the Queries
panel shows one "Orders by region — sqlite_source_N" per source per
iteration, each attributed to its own tool card (per-invocation state).

UI evidence (`media/pr/concurrent-multi-tool/`, captured live via
`tools/agent/capture_parallel_flow.mjs` submitting the prompt through the
real chat box):

- `concurrent-inspect-executing.png` — five "Inspecting orders_N" cards all
  in Executing simultaneously, each with its own Generated Code step.
- `concurrent-create-executing.png` — the five completed ~61s inspect cards
  plus five "Creating Data · Executing" cards spinning at once.
- `final-state.png` — settled conversation, one summary per source.

Sandbox quirks hit while iterating (documented so the loop stays runnable):

- Licensed installs (this sandbox's root bow-config.yaml carries an
  enterprise key) keep the ~60s/tool quota+usage stalls: the best-effort
  usage writes queue their 30s busy_timeout behind the write transaction the
  agent's single-writer session holds during code execution. Concurrency
  still overlaps the stalls (hence 4.8x), but per-tool latency stays ~60s on
  SQLite. Unlicensed installs skip those writes entirely (the
  has_feature gate added in this branch).
- BOW_ENCRYPTION_KEY must be pinned across backend restarts or provider
  credentials become undecryptable (fresh Fernet key per process).
- seed_org must wait for async schema indexing before activating tables
  (fixed in this branch), or create_data resolves zero active tables.

## Loop B' — real Anthropic model (Haiku)

Same probe against a real provider once `ANTHROPIC_API_KEY` is present
(secrets via env only — the sandbox used for this branch had none):

```bash
ANTHROPIC_API_KEY=... uv run python ../tools/agent/run_concurrency_probe.py --anthropic --iterations 5
```

Creates an Anthropic provider (Haiku 4.5, default) through the API. Pair with
`BOW_FORCE_PARALLEL_TOOLS=1` on the backend so Anthropic may emit parallel
tool calls; assertions tolerate variable batch sizes (assert *overlap
happened*, not exact N).

## The fix

- `agent_v2.py`: per-action body extracted into a closure with three locked
  DB sections (start/persist) around an unlocked tool run;
  `_dispatch_action_batch` runs the batch serial (default) or gathered under
  `asyncio.Semaphore(BOW_AGENT_TOOL_CONCURRENCY)` with per-data-source locks
  and a `_PARALLEL_SAFE_TOOLS` gate; accept-cap
  `BOW_AGENT_MAX_ACTIONS_PER_DECISION` (default 10) reports the dropped tail
  to the planner as `not_executed`; post-batch aggregation applies circuit
  breakers/analysis_complete in action order and builds a compact
  `parallel_actions` observation (single-action = verbatim parity).
- `ToolInvocationState` + `inv=` param on `_handle_streaming_event` /
  `_handle_tool_output`: created query/step/visualization attributed per
  invocation; agent adopts the batch's state in action order afterwards.
- `tool.started/progress/stdout/partial/error/finished` SSE events now carry
  `block_id` + `tool_execution_id`; the frontend routes tool events by id
  (`resolveToolEventBlock`) with lastBlock fallback for legacy events.
- `code_execution.py`: `_STDOUT_REDIRECT_LOCK` replaced by a process-wide
  `_ThreadLocalStdoutRouter` (per-thread capture buffers, fallback to real
  stdout); `pptx_executor.py` migrated off `redirect_stdout` too.
- `ToolRunner.validation_failure_counts` per tool name.
- Observation history: `loop_index` tagging, same-iteration compaction
  exemption, iteration-aware `_compact_past_observations` keep-full window.
- `usage_policy_service.py`: data-quota writes gated on
  `has_feature("usage_limits")` (mirrors `add_tokens`).

## What this proves / regression notes

- One planner decision with N tool calls executes all N with per-action
  blocks, tool_executions, and attribution — concurrently when enabled,
  byte-compatible serially when not (cap=1 default).
- SQLite single-writer mode survives concurrent dispatch: all DB work
  serializes behind `_tool_db_lock`; only codegen + code execution overlap.
- Pre-existing failures: 17 unit tests fail identically on the pristine base
  (whatsapp, permissions registry et al) — unrelated to this change.
- The 60s/query quota stall was pre-existing on SQLite (documented in
  `code_execution.py` as "best-effort"); the feature gate removes it for
  unlicensed installs, and licensed installs keep metering (they were already
  paying the contention cost by design).
