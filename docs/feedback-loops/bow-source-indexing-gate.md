# Feedback Loop — BOW source refused every query while history was "still indexing"

A training-mode dashboard over `bow.tool_calls` / `bow.runs` returned no data.
Every `ds_clients["bow"].execute_query(...)` raised
`BOW history is still indexing; retry after indexing completes`, and retrying
never helped.

`BowSourceService.query` counted *pending* runs (not yet stamped with the
current `rollup_version`) in the time window and raised if there was even one.
The count used only the org / window / scope clauses, not the query's own
`agent:` / `tool:` filter, so one unindexed run anywhere in the org blocked
every query that covered it. Nothing guaranteed that such a run would ever be
indexed before the next process restart.

## How runs stay unindexed

Runs are indexed by hooks (run finish, feedback, judge scores) and by a sweep
that ran **once per process start**. Three paths were reproduced:

1. **Run orphaned at birth.** The backend dies (deploy, crash, OOM) before the
   run's first hook. The run stays `in_progress` with `rollup_version = NULL`.
   It only counts as pending once it is older than `STALE_AFTER` (1 h). By
   then, the startup sweep of the restart that orphaned it has already run and
   skipped it.
   (A run that dies *after* early judge scoring is already indexed, because
   `update_completion_scores` calls `refresh_for_completion`. That run does not
   block.)
2. **MCP tool calls.** `MCPTool._finish_tracking` finished the run without the
   rollup hook, so every MCP call left an unindexed run behind.
3. **A run the rollup cannot write.** On Postgres, a NUL byte in a run's error
   text or prompt (legal in the source `json` column, illegal in `TEXT`) made
   the batched UPDATE raise. That aborted the whole sweep, so no other run in
   the batch, or older, got indexed. The next restart hit the same row and
   failed the same way. That makes the refusal permanent, even across restarts.

## Reproduction (real stack, GPT-6 Luna, SQLite and Postgres)

Sandbox per `.agents/skills/sandbox-feedback-loop`: backend + Nuxt, sign-up and
the OpenAI provider configured through the UI (GPT-6 Luna as the default
model), and the Music Store demo agent.

1. Ask a normal question in chat. The run finishes `success` with
   `rollup_version = 1`.
2. Send a second question and `kill -9` the backend the moment its
   `agent_executions` row exists. The row stays `in_progress`,
   `rollup_version = NULL`.
3. Restart. The log shows `diagnosis sweep: nothing to index`: the run is
   younger than an hour, so the sweep skips it.
4. Sandbox time travel: move that run's `created_at` back 70 minutes (stands in
   for the hour passing).
5. Open `/reports/new?mode=training` and ask the agent to build a table of
   `create_data` calls from `bow.tool_calls` for the last 7 days.

Before the fix, on both SQLite and Postgres, the UI shows
`Create Data · BOW · bow.tool_calls — Execution error: BOW history is still
indexing; retry after indexing completes`. The agent schedules a `wait` and
retries, and the retry fails identically.

MCP: a real `tools/call create_data` over `/api/mcp` left its run `completed`
with `rollup_version = NULL`.

Poison row (Postgres): one pending run with `\u0000` in its error JSON plus one
normal pending run. `run_sweep` raised
`CharacterNotInRepertoireError: invalid byte sequence for encoding "UTF8": 0x00`,
and neither run was indexed.

## The fix

- `bow_source_service.py`: removed the gate. Unindexed runs are answered the
  same way runs still in progress already were: included, with empty rollup
  columns. Indexing never blocks a query.
- `diagnosis/sweep.py` + `main.py`: the same sweep now also runs on the
  scheduler every 5 minutes (leader only, claimed per fire with
  `claim_scheduled_run`, idle passes log nothing). A run can stay unindexed for
  at most `STALE_AFTER` + 5 minutes, not until the next restart.
- `mcp/base.py`: `_finish_tracking` calls `refresh_after_run`, like
  `finish_agent_execution`.
- `diagnosis/rollup.py`: NUL bytes are stripped from `prompt_text`,
  `error_text` and `feedback_message`. If a batch still fails, its runs are
  indexed one by one and only the failing run stays pending, so one bad row can
  no longer hold back the rest.

## Verification

After the fix, same sandbox, same steps:

- The same training-mode request returns the table (SQLite: 4 calls; Postgres:
  2 calls) while the orphaned run is still unindexed. The backend was started
  with `BOW_DIAGNOSIS_SWEEP=0` so it stayed unindexed.
- A fresh orphan was skipped by the startup sweep at restart, aged past an
  hour, then indexed by the scheduled job about 5 minutes later with no restart
  (`diagnosis sweep: 1 runs to index … indexed 1 runs`), on both databases.
- A real MCP `create_data` (LLM-backed) finishes `completed` with
  `rollup_version = 1`, on both databases.
- The Postgres poison pair: both runs indexed, with the NUL stripped from the
  error text.

Regression tests (fail before, pass after, on SQLite and on Postgres):

- `tests/e2e/rbac/test_bow_source.py`
  - `test_unindexed_runs_never_block_a_bow_query`: abandoned and unhooked
    runs; admin and agent-manager roles; both datasets and aggregates.
  - `test_saved_bow_query_answers_while_history_is_unindexed`: the saved-query
    (dashboard) path.
  - `test_scheduled_sweep_indexes_runs_left_unindexed_after_startup`, and
    `…_skips_a_fire_another_worker_claimed`.
  - `test_sweep_indexes_runs_whose_text_the_database_rejects`: NUL bytes.
  - `test_one_run_the_sweep_cannot_index_does_not_hold_back_the_rest`.
- `tests/e2e/test_mcp.py::test_mcp_tool_runs_are_indexed_when_they_finish`:
  success and error MCP runs.

## Known limits

- Until a run is indexed, filters on rollup columns (`cost`, `tokens`,
  `tools`, free-text over prompt/error, `feedback`, `judge.*`) don't match it,
  as with runs still in progress. The scheduled sweep bounds that window.
- A run whose rollup raises for another reason stays pending (logged on each
  pass) and keeps counting in the explorer's `unindexed` summary. It no longer
  blocks anything.
