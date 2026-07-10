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

## Loop B' — real Anthropic model (Haiku) — PASS, via the org setting

Same probe against a real provider (secrets via env only). The in-flight cap
here comes from the **`ai_tool_concurrency` org setting** set through the
real settings API (`--concurrency N`); `BOW_AGENT_TOOL_CONCURRENCY` stays
unset so the production config path is what's exercised.

```bash
# backend booted with BOW_FORCE_PARALLEL_TOOLS=1 (lets Anthropic emit
# parallel tool_use) and a PINNED BOW_ENCRYPTION_KEY (see gotchas below)
ANTHROPIC_API_KEY=... uv run python ../tools/agent/run_concurrency_probe.py --anthropic --concurrency 1 --iterations 1   # serial control
ANTHROPIC_API_KEY=... uv run python ../tools/agent/run_concurrency_probe.py --anthropic --concurrency 5 --iterations 3   # concurrent
```

### Observed — serial control (`ai_tool_concurrency=1`)

```
iteration 0: {"tools": 5, "max_overlap": 1, "wall_s": 421.73, "sum_s": 421.53}
  inspect_data start=05:33:04.335 dur=123878ms
  inspect_data start=05:35:08.266 dur=96004ms
  inspect_data start=05:36:44.319 dur=67106ms
  inspect_data start=05:37:51.472 dur=65683ms
  inspect_data start=05:38:57.202 dur=68860ms
```

Strictly one-at-a-time: wall ≈ sum, each start follows the previous finish.

### Observed — concurrent (`ai_tool_concurrency=5`, env override unset)

```
iteration 0: {"tools": 6, "max_overlap": 5, "wall_s": 451.59, "sum_s": 711.39}
  inspect_data start=05:40:14.857 dur=67692ms   ┐
  inspect_data start=05:40:14.939 dur=66846ms   │ five starts within
  inspect_data start=05:40:15.024 dur=66932ms   │ ~300ms — 5-way overlap
  inspect_data start=05:40:15.093 dur=67297ms   │
  inspect_data start=05:40:15.161 dur=67000ms   ┘
  create_data  start=05:41:30.826 dur=375620ms
iteration 1: {"tools": 6, "max_overlap": 3, "wall_s": 335.49, "sum_s": 446.39}
  ... three create_data starts within ~270ms (05:52:22.373/.511/.641), all success
iteration 2: single search_instructions, no fan-out (real-model variability)
```

The inspect phase that took ~420s serial completed in ~67s wall — ~5–6× on
the phase. Haiku emitted variable batch shapes across iterations (5-way
inspect, 3-way create, no fan-out), as expected of a real model; the
assertion is *overlap happened at depth > 1*, which held (max depth 5).
A few individual tool executions failed (Haiku codegen fumbles) and were
retried/recovered by the normal error paths — iteration 1 was fully green.

### Live UI capture (real Haiku, fresh report)

A fully green 10/10 run on a fresh report (fresh context forces the fan-out;
a report that already contains the summaries gets answered from context with
zero tools — real-model behavior worth knowing when reproducing):

```
inspect_data ×5  starts 06:03:29.530 → 06:03:30.055 (~525ms spread), all success, ~124s each
create_data  ×5  starts 06:05:44.043 → 06:05:44.599 (~556ms spread), all success, ~66s each
```

Captured through the real chat box via
`tools/agent/capture_parallel_flow.mjs` (`media/pr/concurrent-multi-tool/live-haiku/`):

- `five-inspect-executing-live-haiku.png` — five "Inspecting orders_N" cards
  streaming in Executing state simultaneously, Haiku model badge visible.
- `five-create-executing-live-haiku.png` — five "Creating Data · Executing"
  cards simultaneously.
- `final-state-live-haiku.png` — completed turn: per-source region summaries,
  5 queries, knowledge suggestion, follow-ups.

(Note for reruns: `capture_parallel_flow.mjs` resolves `@playwright/test`
from `tools/agent/`, which requires the repo-root `node_modules` symlink →
`frontend/node_modules`; create it with `ln -sfn frontend/node_modules
node_modules` — it's gitignored.)

## Loop B''' — org setting alone (no env flag) — PASS

Final proof that `ai_tool_concurrency` controls the whole feature: backend
restarted with **no `BOW_FORCE_PARALLEL_TOOLS`** (verified absent from the
process env), org setting = 5 via the API, Haiku, fresh report:

```
iteration 0: {"tools": 12, "max_overlap": 5, "wall_s": 212.22, "sum_s": 777.12}
  wave 1  07:41:03.376–.667  5× inspect_data within ~290ms (3 ok, 2 failed)
  wave 2  07:42:17.357–.664  3× create_data + 2× inspect_data RETRIES within ~310ms
  wave 3  07:43:30.202–.339  2× create_data
```

Wave 2 is the batch semantics working end-to-end: the failed inspects from
wave 1 did NOT trip the circuit breaker (batch counts one round), and the
planner retried them concurrently alongside the creates it was already
ready to issue. 10/12 tools green, ~3.7× wall-clock vs serial-sum.

## Loop B'' — cross-provider matrix (real keys, one probe run each)

`ai_tool_concurrency=5` via the org setting; backend with
`BOW_FORCE_PARALLEL_TOOLS=1` (now honored by anthropic + openai + azure +
responses clients; google/bedrock never send a disable flag).

| Provider | Model | LLM served | Parallel emission | Concurrent dispatch | Tool success |
|---|---|---|---|---|---|
| Anthropic | claude-haiku-4-5 | ✓ | ✓ 5-way inspect + 5-way create | ✓ depth 5 | 10/10 (capture run) |
| OpenAI | gpt-5.4-mini | ✓ | ✓ 5-way (stochastic: 1 of 3 runs) | ✓ depth 5 — 127s wall vs 633s sum | 0/5 — codegen guessed `orders` for `orders_N` |
| OpenAI | gpt-5.4 | ✓ | ✗ this run (one per turn) | n/a | 6/6 green, serial |
| Azure Foundry | gpt-5.4-mini | ✓ via Responses `/openai/v1` | ✗ this run | n/a | inspect failed (mini codegen) |
| Google | gemini-flash-latest | ✓ | ✗ this run | n/a | create_data missing required `title` |
| Bedrock | claude-3-5-haiku | ✗ — AWS keys rejected (`UnrecognizedClientException`) | — | — | — |

Reading: the dispatch/execution layer is provider-agnostic — wherever a
batch formed (Anthropic, OpenAI) it ran at depth 5 through the org-setting
path with per-action blocks and attribution. Whether a batch FORMS is model
behavior: Anthropic batches reliably under the relaxed prompt, gpt-5.4-mini
sometimes, gpt-5.4/gemini-flash preferred serial in their single runs.
Tool success is model codegen quality, orthogonal to concurrency.

The OpenAI mini run also caught a real dispatch-semantics bug in the wild:
all 5 batch members failing tripped the 3-strike failure breaker meant for
consecutive failed iterations, ending the turn before the planner could
correct the table names — fixed by counting one failed round per tool per
batch (`_batch_failure_rollup`).

Provider-specific gotchas from this leg:

- Google: `gemini-2.5-flash` is retired for new users (404 with the model
  still listed by ListModels); use `gemini-flash-latest`.
- Azure Foundry resources have no `/openai/deployments/<name>` routing for
  catalog models (DeploymentNotFound); they serve models by NAME on
  `/openai/v1` — set `use_responses_api` on the provider.
- Switching the org's serving model requires the model-level
  `/api/llm/models/{id}/set_default`; creating a provider whose model has
  `is_default: true` does NOT unseat the existing default (first "OpenAI"
  run silently served on Haiku).

### Sandbox gotchas (cost us one dead run each)

- **Pin `BOW_ENCRYPTION_KEY`** before the first boot. When unset, the backend
  generates a fresh key per process (`app/settings/config.py:73-76`), so any
  backend restart makes previously stored LLM-provider credentials
  undecryptable (`InvalidToken` at `llm_provider.py:95`) and every completion
  dies at planner init in ~6s with zero tools executed.
- Providers whose models are org defaults can't be deleted via the API
  (400 "Cannot delete models that are set as default"); in a sandbox, clear
  `llm_models` + `llm_providers` rows directly and let the probe recreate.
- The backend must run with `BOW_FORCE_PARALLEL_TOOLS=1` or the Anthropic
  client sends `disable_parallel_tool_use=true` and no batch ever forms.

## The fix

- `agent_v2.py`: per-action body extracted into a closure with three locked
  DB sections (start/persist) around an unlocked tool run;
  `_dispatch_action_batch` runs the batch serial (default) or gathered under
  an `asyncio.Semaphore` sized by the **`ai_tool_concurrency` org setting**
  (default 1; `BOW_AGENT_TOOL_CONCURRENCY` env var is an ops/sandbox
  override) with per-data-source locks
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
