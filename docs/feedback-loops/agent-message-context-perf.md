# Feedback Loop — agent message context takes tens of seconds on large reports

Agent completions on reports with stored query results became progressively
slower even though the bounded conversation-history query itself was fast. The
claim validated here is that message rendering was hydrating unrelated report
relationships, including every stored `steps.data` payload, and that the agent
loop then repeated the same message build.

## Root cause (validated)

`Completion.report` and most `Report` collections use mapper-level
`lazy="selectin"` loading (`backend/app/models/completion.py:45`,
`backend/app/models/report.py:105-122`). The bounded Completion queries in
`MessageContextBuilder` did not override those defaults. Loading recent
completions therefore expanded through Report into widgets, queries, historical
steps, artifacts, files, and the rest of the report graph.

The digest does not need that graph. It obtains completion blocks and tool
executions through explicit batched queries. Nevertheless, the agent loop also
rebuilt messages immediately after `refresh_warm()` and invoked the legacy full
context builder after post-tool and final refreshes.

## Loop A — deterministic reproduction

The regression test seeds a report with stored step data, ordinary conversation
turns, and a `create_data` tool execution. It records SQL while building message
history and asserts both sides of the contract:

- the explicit `50 rows × 1 cols` tool digest is still rendered;
- message rendering never queries `steps`.

```bash
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db \
  uv run pytest \
  tests/unit/test_context_compaction.py::test_message_builder_does_not_hydrate_report_step_data \
  -q
```

Before the fix:

```text
FAILED test_message_builder_does_not_hydrate_report_step_data
AssertionError: bounded message history hydrated report step data via an ORM relationship cascade
```

## The fix

- Every Completion entity query in
  `backend/app/ai/context/builders/message_context_builder.py` now applies
  `lazyload("*")`; scalar-only watermark/count queries avoid entity hydration
  entirely.
- `backend/app/ai/agent_v2.py` reuses `view.warm.messages` after
  `refresh_warm()` and no longer invokes the ignored legacy full-context build
  after post-tool or final warm refreshes.
- Tool-execution digest construction is unchanged.

After the fix:

```text
1 passed
```

Related agent/context verification:

```bash
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db uv run pytest \
  tests/unit/test_context_compaction.py \
  tests/unit/test_session_events.py \
  tests/unit/test_agent_loop_rescue.py \
  tests/unit/test_concurrent_tool_dispatch.py -q
```

Observed: `82 passed`.

## Loop B — localhost smoke

With the development app running at `http://localhost:3000`, start a new Chat
report and send `Reply with exactly: local performance smoke passed.`. The UI
must render the exact response without browser errors.

Observed on the local SQLite-backed stack:

| Turn | Total agent duration | First token | Result |
| --- | ---: | ---: | --- |
| 1 | 8.2 s | 3.2 s | success |
| 2 | 6.1 s | 2.0 s | success |

The values come from `agent_executions`; both corresponding completions were
also persisted with `status = success`.

## What this proves / regression notes

The test guards the general invariant: the amount of stored step data cannot
affect bounded message-history construction, while explicit tool digests remain
available to the planner. The later persisted-summary loop in
`context-loading-efficiency.md` closes the remaining cold-start gap: new
executions write their bounded history projection to
`ToolExecution.context_summary_json`, and legacy rows retain the SQL-projection
fallback for one read. That bounded projection is then written through, so a
fresh later builder no longer parses the historical large JSON.
