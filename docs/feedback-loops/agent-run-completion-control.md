# Feedback Loop — Agent stops after small errors or an unfinished analysis

This loop reproduces and verifies three related harness failures: an ordinary
tool error could end a run, a planner `end_turn` could be recorded as success
while multi-step work remained unfinished, and a healthy but quiet warehouse
query could be killed by the tool idle timeout. It also pins the required
counter behavior: failures of the same tool in distant steps are historical
events, not one consecutive failure streak.

## Root cause (validated)

The harness mixed three different concepts into `analysis_complete`:

- `ToolRunner` retry exhaustion returned a terminal observation, allowing one
  tool boundary to decide the outcome of the entire run. The corrected return
  contract is now non-terminal at
  `backend/app/ai/runner/tool_runner.py:139-206` and
  `backend/app/ai/runner/tool_runner.py:360-377`.
- The agent counted failures by tool name for the whole run. That let two
  distant `inspect_data` failures combine even when the intervening work made
  progress. The replacement tracks a hashed `tool + arguments` signature only
  across adjacent planner rounds at `backend/app/ai/run_control.py:32-130`.
- Planner `end_turn + no action` still correctly produces a completion
  candidate, but it previously flowed directly into success. The outer harness
  now evaluates the current execution's untruncated `Plan` note before
  persistence at `backend/app/ai/agent_v2.py:4615-4676`.
- The first live probe found a provenance integration gap: `create_note`
  expected an execution object, while the live runtime supplies
  `agent_execution_id`. The note was therefore saved with a null execution ID
  and was invisible to the current-run checklist query. `create_note` now
  accepts either runtime shape at
  `backend/app/ai/tools/implementations/create_note.py:90-95`.
- `StreamingCodeExecutor` emitted one `data_query_execution` progress event and
  then awaited the warehouse driver silently. In the production reproduction,
  a healthy query took 171.3 seconds—only 8.7 seconds below `ToolRunner`'s
  180-second idle limit—while three other runs were killed after two silent
  attempts. The shared executor now emits a stage-preserving progress heartbeat
  every 30 seconds in `backend/app/ai/code_execution/code_execution.py`.
- The existing hard-timeout task was never raced or awaited, so adding
  heartbeats would have allowed a truly stuck stream to run forever. The runner
  now wraps the stream in an absolute deadline and cancels pending iterator
  tasks at `backend/app/ai/runner/tool_runner.py`.

The important boundary is that tools and planner turns produce observations or
completion candidates; only the outer run controller accepts success.

## Loop A — deterministic reproduction

Run from `backend/`:

```bash
TESTING=true uv run pytest -q \
  tests/unit/test_tool_runner_validation.py \
  tests/unit/test_agent_run_control.py \
  tests/unit/test_code_execution_heartbeat.py \
  tests/unit/test_tool_runner_timeouts.py
```

Before the fix, the observed failures were:

```text
KeyError: 'retry_exhausted'
ModuleNotFoundError: No module named 'app.ai.run_control'
TimeoutError: quiet code generation/query produced no heartbeat
TimeoutError: heartbeat stream ignored the unobserved hard-timeout task
ImportError: missing bounded completion-review policy
```

The existing observation instead contained `analysis_complete=True` and a
forced `final_answer`. No controller existed to distinguish adjacent identical
approaches, current-run notes, or an unfinished completion contract.

## The fix

1. Tool validation, timeout, runtime, and retry-exhaustion errors return
   `success=False`, structured error details, `retry_exhausted=True`, and a
   `change_strategy` suggestion. They never return `analysis_complete` or a
   final answer.
2. `ApproachFailureTracker` counts only identical tool+argument failures in
   adjacent planner rounds. Three adjacent failures annotate the observation
   with `approach_exhausted`; the agent continues and must change strategy.
3. Notes titled exactly `Plan`, authored by the agent in the current
   `agent_execution_id`, form the deterministic completion checklist. Old-run,
   user-authored, and non-Plan notes cannot block completion.
4. A planner end-turn is rejected when an existing Plan has unchecked items.
   Missing Plan notes never block completion: there is no deterministic
   checklist to enforce. Unchecked Plans get at most two review attempts before
   liveness wins, so the checklist gate cannot consume the global step budget.
   Rejected candidates are persisted with `phase=completion_review` so the loop
   remains visible in production diagnostics.
5. Agent-authored notes preserve the live execution ID, so the gate reviews
   only the checklist created by the run that is trying to finish.
6. Generated-code callers share a 30-second heartbeat during both code
   generation and query execution. The pulse keeps the current UI stage and is
   excluded from phase timing.
7. Heartbeats reset only the idle timeout. An absolute hard deadline still
   cancels the tool stream and returns a non-terminal failure observation for
   replanning.

## Verification

Focused regression suite:

```bash
TESTING=true uv run pytest -q \
  tests/unit/test_agent_run_control.py \
  tests/unit/test_code_execution_heartbeat.py \
  tests/unit/test_tool_runner_timeouts.py \
  tests/unit/test_tool_runner_validation.py \
  tests/unit/test_concurrent_tool_dispatch.py \
  tests/unit/test_artifact_feedback_loop.py \
  tests/e2e/test_agent_notes.py
```

Observed after the fix: exit code `0` (`78` tests passed).

Broader unit suite:

```bash
TESTING=true uv run pytest -q tests/unit
```

Observed after the fix: exit code `0`.

## Loop B — live localhost verification

With the backend on `localhost:8000`, frontend on `localhost:3000`, and the
deterministic OpenAI-compatible stub on `localhost:9099`, run from `backend/`:

```bash
uv run python ../tools/agent/run_control_probe.py
```

The probe uses the real auth, provider, organization settings, report, and
completion APIs, then checks persisted completions, executions, tool calls,
and notes in the local SQLite database. Its scripted planner deliberately:

1. sends an invalid `create_note`,
2. creates a three-item unchecked Plan,
3. performs three research rounds,
4. tries to finish while the Plan is unchecked,
5. checks every item after the harness rejects that answer,
6. finishes again.

Observed after the fix:

```text
completion_status: success
execution_status: success
planner_calls: 8
create_note_successes: [false, true]
tools: create_note, create_note, search_agents x3, edit_note
Plan: 3 checked items, 0 unchecked items
PASS: localhost run recovered from a tool error and rejected premature completion
```

The accepted completion contains `Verified completion after checklist
reconciliation` and does not contain the rejected `Premature answer`.

## What this proves / regression notes

- A generic tool failure is evidence for the next planner turn, not a terminal
  decision.
- Step 1 and step 55 failures of the same tool do not form a two-strike streak.
- Only three adjacent failures of the same normalized approach trigger the
  strategy-change warning, and even then the run remains active.
- Simple tasks without a Plan still finish normally.
- Missing Plans never deadlock completion. Existing unchecked Plans trigger at
  most two reconciliation loops, and each rejection is persisted for audit.
- Explicit control tools and product policies that intentionally end or pause
  a turn retain their existing behavior; this change targets ordinary errors
  and premature planner completion.
- Quiet codegen and warehouse execution remain visibly in their current stage,
  reset the idle watchdog, and remain bounded by the hard deadline.

## Production regression caught after the first fix

Report `6439421c-dc4c-4dad-ba64-bbd355a5d701` on production version `0.0.543`
completed 18/18 tool executions successfully, updated the target instruction
through v17, and then failed after 846 seconds with `planner_step_limit`.
The run used all 100 planner iterations but persisted only 11 action decisions
and zero notes. The missing iterations were completion candidates repeatedly
rejected by the new `plan_required` rule, then deleted with their UI skeletons.

This proved two liveness/observability defects in the first fix: a Plan could be
required retroactively after useful work, and `completion_review_count` had no
bound. The follow-up regression tests require missing Plans to pass immediately
and cap unchecked-Plan rejection at two attempts.
