# Feedback Loop — "eval run can take some time … does the agent wait ten minutes and then read the results?"

Investigating the chat-driven eval loop (create eval → run → read results →
iterate) surfaced something deeper than the reported blocking behavior: a
TestRun launched in the background (`POST /api/tests/runs/batch`, which is
also what the `run_eval` tool calls) **never finishes** unless a client
streams it. The agent's `run_eval` blocked the whole loop polling for
results that could only appear if a human happened to have the run page
open; with nobody watching, the tool burned its timeout and errored the run.

## Root cause (validated)

- Evaluation (`TestEvaluationService.evaluate_final` → `persist_result_json`)
  was invoked **only** inside `TestRunService.stream_run`
  (`test_run_service.py`, the SSE handler behind the run detail page).
- Run finalization (status/summary_json) lived **only** in `stream_run`'s
  streamer loop — reachable, again, only while a client held the stream open.
- `create_and_execute_background` dispatched agent completions and returned;
  nothing evaluated the finished reports, so `TestResult` rows stayed
  `in_progress` and `TestRun.status` stayed `in_progress` forever.
- The `run_eval` tool polled those never-changing rows while blocking the
  agent loop, emitting progress events only on transitions — so
  `ToolRunner`'s 180s idle timeout (`agent_v2.py` TimeoutPolicy) could kill
  and *retry* the tool, launching a duplicate TestRun.

## Loop A — deterministic reproduction

Environment: `tools/agent/boot_stack.sh --dev`, then
`uv run python ../tools/agent/seed_org.py --demo` and an LLM configured from
`$ANTHROPIC_API_KEY` (see Loop B note). Reproduction script: create one eval
case (`tool.calls create_data min_calls=1` on the demo Music Store DS),
`POST /api/tests/runs/batch`, and poll run/results/status **without ever
opening the stream**.

Observed BEFORE the fix:

```
run: 29c79ecc-… status: in_progress
t+0s  run=in_progress results=['in_progress'] system_completions=['in_progress']
t+10s run=in_progress results=['in_progress'] system_completions=['success']   <- agent done
TIMEOUT: run still in_progress after 240s — background runs never finalize
without a streaming client (BUG REPRODUCED)
```

The agent finished in ~10s; the result stayed `in_progress` forever.

## The fix

`TestRunService` gains a **background finalizer** armed by
`create_and_execute_background` (`_watch_and_finalize`): per case it waits
for the agent turn(s) to reach a terminal completion, evaluates expectations
(same recipe as `stream_run`'s terminal branch, judge included), persists the
TestResult, then finalizes the run (`_finalize_run_if_done`) and fires the
run-finished wake (`_maybe_fire_wake`) — a completion posted back to the
conversation that started the run (new `TestRun.origin_report_id` /
`origin_user_id` / `wake_on_finish` columns, migration `evalwake0001`).
`stream_run` keeps working for live UI and now also polls the DB every ~2s so
it observes results persisted by the finalizer (or another worker).

On top of that engine fix, the tool surface changed to the background-first
design (docs/design/eval-agent-loop.md):

- `run_eval` defaults to `wait_s=0`: returns `{detached: true, run_id}`
  immediately; `wait_s>0` stays attached with live progress plus a 30s
  heartbeat (defeats the idle-timeout kill/retry), detaching instead of
  stopping the run when the budget runs out.
- New tools: `get_eval_runs` (list), `get_eval_run` (detail +
  `compare_to_previous`), `stop_eval_run`, `cancel_wait`, `edit_eval`;
  `search_evals` now matches expectations/tags and returns judge excerpts.
- Run hygiene in the service: identical in-progress run (same build + case
  set) is reused (`deduped`), org concurrency cap
  (`max_concurrent_eval_runs`, default 3, stale runs excluded).
- UI: the chat run card polls the run API for detached runs (stop button
  included); the run detail page gained a build-over-build comparison panel
  (`GET /api/tests/runs/{id}/compare`).

Observed AFTER the fix (same script, no streaming client):

```
run: 99ec183c-… status: in_progress
t+0s  run=in_progress results=['in_progress'] system_completions=['in_progress']
t+10s run=success results=['pass'] system_completions=['success']
RUN FINALIZED — background path works without streaming
```

## Loop B — live confirmation (real LLM)

With Claude Haiku configured from `$ANTHROPIC_API_KEY` (env var only): send a
training-mode chat prompt "Create an eval … then run it" and observe, on one
report: agent turn 1 (`create_eval` → `run_eval` detached) → background run
executes and finalizes → `[Eval run finished]` wake completion arrives →
woken agent calls `get_eval_run` and reports pass/fail. Verified 2026-07-16.

Loop B also caught a real integration bug the deterministic loop couldn't:
the first wake completion resumed the agent in default **chat** mode, where
the training-only `get_eval_run` isn't in the catalog — the woken agent
flailed into `execute_mcp` instead. Fix: the wake prompt pins
``mode='training'`` (run_eval is training-only, so the origin conversation
is always a training session). Second live pass: woken turn calls
`get_eval_run:success` and reports "1/1 passed".

## Regression tests

`backend/tests/unit/test_eval_loop_tools.py` — `wait_s` bounds and
background default, detached output shape, input schemas for the new tools,
`cancel_wait` against a mocked WaitService, and digest coverage. Existing
`test_run_eval_input.py` / `test_wait_tool.py` still pass unchanged
(43 tests total).

## What this proves / notes

- Background eval runs are self-contained: evaluate, finalize, and notify
  without any streaming client or open browser tab.
- The chat loop (create → run → wake → read → edit instructions → rerun →
  compare) closes end-to-end without blocking the agent.
- Pre-existing runs stuck `in_progress` from before this change have no
  watcher; they're excluded from the concurrency cap once older than the
  watch timeout and can be cleaned up with stop.
