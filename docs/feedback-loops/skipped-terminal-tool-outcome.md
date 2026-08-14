# Feedback Loop — artifact limit rejection caused a planner loop

A production dashboard run reached the artifact-call safety limit, but the
planner continued requesting `edit_artifact` until the provider ran out of
credit. This loop validates the general contract that a policy-skipped tool
outcome carrying `analysis_complete=true` must terminate the run.

## Root cause (validated)

`backend/app/ai/agent_v2.py:5159` returns artifact-budget rejections as skipped
outcomes with a terminal observation. The outcome aggregation loop at
`backend/app/ai/agent_v2.py:5695` discarded every skipped outcome before the
finalizer at `backend/app/ai/agent_v2.py:5766` could read `analysis_complete`,
so the outer planner loop continued. Non-terminal skips should remain ignored,
while terminal skips must flow through the existing finalization path.

## Loop A — deterministic reproduction (no external services)

Run from `backend/`:

```bash
UV_CACHE_DIR=/tmp/bow-uv-cache uv run pytest -q \
  tests/unit/test_concurrent_tool_dispatch.py::test_policy_skipped_terminal_outcome_still_stops_the_run
```

Before the fix, the test fails because `AgentV2` has no mechanism for
distinguishing a terminal skipped outcome from an ordinary skipped action:

```text
AttributeError: type object 'AgentV2' has no attribute '_outcome_ends_run'
```

## The fix

`AgentV2._outcome_ends_run` recognizes an outcome whose observation has
`analysis_complete=true`. The aggregation loop continues to ignore ordinary
skips, but allows terminal skips through the existing final-answer persistence,
completion-finished event, and outer-loop termination path.

After the fix:

```text
1 passed
```

## What this proves / regression notes

The regression covers policy outcomes rather than one artifact ID or one model:
a refused action can terminate a run, while a non-terminal skipped action does
not. No LLM, database, browser, or production credentials are required.
