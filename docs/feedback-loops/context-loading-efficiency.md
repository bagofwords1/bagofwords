# Feedback Loop — Large stored context does not repeat prompt-startup work

Prompt construction needs bounded metadata and previews, but previously some
paths loaded complete ORM graphs and decoded complete stored result payloads.
This loop verifies that prompt startup remains bounded without removing any
planner-visible context.

## Root cause (validated)

Several metadata-only operations selected ORM entities with broad `selectin`
relationships. Conversation history also selected complete
`ToolExecution.result_json` values to render bounded `create_data` and
`read_query` digests, while query context selected complete `Step.data` values
to render dimensions, statistics, and five preview rows.

The bounded loading paths are implemented in:

- `backend/app/services/completion_service.py`;
- `backend/app/ai/context/builders/message_context_builder.py`;
- `backend/app/ai/context/builders/query_context_builder.py`;
- `backend/app/ai/context/summary_write_through.py`.

## Loop A — deterministic reproduction

The regression suite seeds oversized later rows that must never be decoded by
the summary-backed path. It also creates legacy null-summary rows and verifies
that their first bounded projection is written through for future builders.

```bash
cd backend
TESTING=true uv run pytest \
  tests/unit/test_message_context_tool_result_projection.py \
  tests/unit/test_query_context_step_summary.py \
  tests/e2e/test_step_retention_purge.py \
  --db=sqlite -q --disable-warnings --tb=short
```

Before read-through persistence was implemented, the three legacy-row
regressions failed because `context_summary_json` remained null after the first
build. After the fix, the complete suite reports `20 passed` on SQLite and
`20 passed` on PostgreSQL.

Related context-compaction, agent-output, completion-loading, and
artifact-loading regressions report another `25 passed`:

```bash
cd backend
TESTING=true uv run pytest \
  tests/unit/test_context_compaction.py \
  tests/unit/test_agent_read_only_tool_output.py \
  tests/unit/test_completion_stream_bounded_loading.py \
  tests/unit/test_artifact_relationship_loading.py \
  -q --disable-warnings --tb=short
```

## The fix

- New `Step.data` and supported `ToolExecution.result_json` writes atomically
  store the exact bounded projection consumed by prompt builders.
- Summary-backed history never selects `tool_executions.result_json`;
  summary-backed query context never selects `steps.data`.
- A legacy row uses the existing projection once and writes the already-bounded
  result through an independent, one-second-bounded transaction.
- Tool write-through is terminal-only. Both models guard on `updated_at` and
  `context_summary_json IS NULL`; Step write-through also requires that
  retention has not purged its source data.
- The migration adds nullable columns only. It performs no bulk historical
  rewrite, and failures remain non-fatal so a later read can retry.

## What this proves / regression notes

The tests prove that a fresh builder renders identical context after
write-through without referencing the large JSON column. They also prove that
nonterminal tool results are not frozen and that a stale Step projection cannot
overwrite a rerun or recreate retained-away data.

The first access to a legacy row still pays its original projection cost once.
Subsequent accesses use the persisted summary.
