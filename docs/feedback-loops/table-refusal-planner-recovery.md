# Feedback loop — table refusal ends the conversation with a truncated error

An analyst asks for annual sales and best-selling products. A query references a table disabled for the agent, despite an enabled cached alternative appearing in the schema. Previously the refusal ended the entire conversation and displayed a truncated English diagnostic. The query must fail, while the planner retains control to explain the limitation in the user's language and finish permitted work.

## Root cause (validated)

Reported conversation: https://app.bagofwords.com/reports/3a9b3236-352a-4887-8dc9-61d8f03e45e7.

Read-only investigation on November 4, 2026 found native `Album` inactive and cached `album_1` active (`select * from album`). The saved describe_tables response offered `album_1`, Invoice, InvoiceLine and Track. Older instructions still named Album, and the generated query used the native SQLite Album table. Table-selection enforcement, shipped in v0.0.601 / PR #1522, correctly refused it. Nothing in production was changed.

`backend/app/ai/code_execution/code_execution.py` marks TableAccessError terminal so a generated function cannot catch the denial and silently return fallback data. Query timings previously carried only a truncated diagnostic and the terminal bit. `CreateDataTool` treated every terminal query failure as an infrastructure failure, marked analysis_complete, and copied the 200-character timing diagnostic into final_answer. `agent_v2.py` consumes those fields to end the conversation immediately, without another planner turn.

## Loop A — deterministic reproduction

Use Python 3.13 with the backend development dependencies installed (`uv sync --frozen --extra dev`). From backend:

```bash
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true uv run pytest \
  tests/unit/test_query_failure_observation.py \
  tests/unit/test_table_scope_guard.py \
  tests/unit/test_swallowed_query_error.py \
  tests/unit/test_bow_source_contract.py \
  tests/unit/test_query_timeout.py \
  -q --disable-warnings --tb=short
```

The new regression test supplies code through the executor's code-generator boundary, creates an isolated SQLite source, and runs the real subprocess sandbox, SQL guard, query wrapper, and failure-observation builder. No model credentials or application database are needed. It covers multiple disabled table names, direct failures, and generated functions that catch a refusal and return nonempty permitted data. Both must fail the attempt, avoid automatic code-generation retries, and leave analysis open for the planner. A separate later permitted query must succeed. Infrastructure failures must still end analysis, including caught failures. Ordinary query errors remain correctable.

Before the behavior change, with the existing observation assembly extracted unchanged into its shared builder:

```text
4 failed, 2 passed
AssertionError: assert 'infrastructure_failure' == 'table_access_denied'
```

Two additional tests cover a refusal followed by a different nonterminal error, and ordinary correctable failures.

## Fix

- Query timings retain a typed `table_access_denied` classification and the complete refusal message, alongside the existing bounded timing diagnostic.
- `CreateDataTool.build_execution_failure_observation` returns a refusal observation and recovery guidance without analysis_complete, final_answer or conversation-terminal flags. An earlier terminal query failure takes precedence over later nonterminal errors.
- Query-level terminal handling remains intact: generated code cannot save fallback results as success, and code generation does not retry the refused attempt. Genuine infrastructure failures retain their prior conversation-terminal behavior.
- Planner and both coder prompt paths prioritize exact active table names and their listed clients over names in instructions or interpreted_prompt, including cached tables. Recovery guidance asks the planner to explain limitations in the user's language and continue independent permitted work.

## What this proves / limits

The focused suite passes 356 tests, with zero failures. The new test file passes lint and the diff passes whitespace checks. These are deterministic tests of the execution/observation boundary, not a live-model evaluation: production replay, Hebrew answer quality, actual selection of album_1 and cross-client merging have not been verified. The prompts guide those model decisions; the runtime fix guarantees that this refusal no longer forces a raw diagnostic into the final answer.

No frontend files or permissions/configuration were changed. No deployment, version bump, or production table reselection is part of this change.
