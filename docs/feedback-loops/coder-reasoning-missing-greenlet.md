# Feedback loop — "Agent failed: greenlet_spawn has not been called" on Postgres

A customer on a Postgres backend asked for several tables at once. The planner
issued three parallel `create_data` calls, and the run died with
`Agent failed: greenlet_spawn has not been called; can't call await_only() here`.
It is a regression from `a7a8ca7` (stream provider reasoning summaries).

## Cause

1. `AgentV2._coder_reasoning_callback` saved each coder reasoning snapshot
   (~every 1.2s) through the agent's **shared** `AsyncSession` (`self.db`):
   `get → commit`, and `rollback` on failure. It ran while sibling tools of
   the same parallel batch used that same session. Some of those reads do not
   take `_tool_db_lock` (for example `create_data`'s viz-instruction build and
   `LoadablesResolver`).
2. On Postgres, asyncpg rejects two operations on one connection at once
   (`cannot perform operation: another operation is in progress`). SQLite's
   driver quietly queues them instead, so the bug only appeared on Postgres.
3. A failed snapshot commit made the callback call `self.db.rollback()`. A
   rollback expires **every** instance in the session, even with
   `expire_on_commit=False`.
4. `ProjectManager.next_seq` read `agent_execution.latest_seq`. On an expired
   instance, that read tries to lazy-load from the database, which raises
   `MissingGreenlet` under asyncio. It failed again on every later SSE event.
   The loop's crash handler only reloaded objects when `not db.is_active`, but
   after the rollback the session *was* active again. So every retry hit the
   same error and the run failed. The rollback also discarded the uncommitted
   in-memory seq, so SSE sequence numbers could repeat.

## Fix

- `backend/app/ai/agent_v2.py` `_coder_reasoning_callback`: reading and
  saving reasoning uses a short-lived session of its own (`self._session_maker`,
  a single `UPDATE`). It never commits or rolls back `self.db`. If the block is
  already loaded in the shared session, that copy gets the new text as a
  committed value in memory (`set_committed_value`), with no IO and nothing
  marked dirty. The execution id is read from the identity, which cannot
  trigger a load.
- `backend/app/project_manager.py` `next_seq`: the live counter is an
  unmapped attribute that a rollback cannot expire. It is seeded from the
  loaded column without triggering a load. The counter keeps increasing
  across rollbacks, and `latest_seq` is still flushed by the agent's own
  commits.
- `backend/app/ai/agent_v2.py` loop crash handler: also reloads the core
  objects when any of them is expired, not only when the transaction is
  broken.

## Loop A — deterministic reproduction on real Postgres

`tools/agent/repro_reasoning_greenlet.py` binds the **real**
`_coder_reasoning_callback` to a thin agent stub on a real asyncpg session. A
sibling coroutine plays a parallel `create_data`.

```sh
cd backend && BOW_DATABASE_URL=postgresql://bow:bow@localhost:5432/bow \
  uv run python ../tools/agent/repro_reasoning_greenlet.py [--commit-fails-once|--poison|--external-rollback]
```

| mode | before (HEAD) | after |
|---|---|---|
| concurrent (unlocked sibling reads) | asyncpg `another operation is in progress`, session stuck `prepared`; one run hung until the 300s timeout | 0 errors |
| `--commit-fails-once` | **58× `MissingGreenlet: greenlet_spawn has not been called`** (the customer's exact error), including the agent's own `next_seq` afterwards | 0 errors |
| `--poison` (sibling statement fails, error swallowed) | 58× `PendingRollbackError` | 0 errors |
| `--external-rollback` | 62× `MissingGreenlet` in the stream | stream unaffected; only the probe's direct `current_execution.id` read fails, which the loop's crash handler now reloads |

## Loop B — unit contract

`backend/tests/unit/test_stream_session_isolation.py`: the stream seq keeps
increasing across a shared-session rollback, and the coder reasoning stream
writes nothing through the shared session while persisting combined planner
and coder text. **Before: 5 failed (MissingGreenlet), 1 passed (the no-rollback case). After: 6 passed.**
Neighbouring suites (agent-loop rescue, completion stream, reasoning streamer,
context-hub session serialization, title streaming, session events): **62
passed**.

## Loop C — full sandbox, Postgres + Claude Haiku 4.5

Backend on Postgres 16, `tools/agent/seed_org.py --demo` (Music Store),
`tools/agent/setup_haiku_llm.py`, driven through the chat UI with Playwright:
*"think hard. … create these three separate tables in parallel …"* ("think
hard" enables coder thinking).

- Fixed code on local Postgres and through `tools/agent/pg_latency_proxy.py`
  (15ms one-way): three `create_data` calls from one decision ran overlapping
  (13–60s). All were `success`, each block kept 1.9–7.6k characters of
  reasoning, `latest_seq` was persisted, there were zero
  greenlet/asyncpg/rollback errors in the backend log, and the report
  re-renders after a reload.
- Unfixed code: 4 runs (2 local, 2 through the latency proxy) all happened to
  pass. The collision needs a sibling's unlocked read to land on a snapshot
  commit, and it did not trigger in this sandbox. Loop A is the reproduction.

## Not changed

Unlocked shared-session reads inside `create_data` (viz instructions,
`LoadablesResolver`) still exist. With this change, the reasoning stream no
longer competes with them.

## Follow-up — SQLite regression from this fix (and its correction)

Moving coder-reasoning persistence to its own session was right for Postgres but
wrong for SQLite. There single-writer mode is always on: the agent's session is
the only writer and holds the write lock through the tool run, so the second
session waited out `busy_timeout` on every ~1.2s snapshot. The wait is awaited
inside the coder's LLM stream, so parallel `create_data` stalled into the 300s
tool hard timeout (`hard timeout` ×3, retried, ×3 again; the scheduler's own
writes failed with `database is locked` meanwhile).

Reproduced in the SQLite sandbox with the same three-table parallel prompt
(data source attached, Claude Haiku 4.5):

| code | result |
|---|---|
| `40af090` (before this fix) | success, 42s, tools 12–18s |
| `2951f80` (this fix) | all 3 `create_data` hit the 300s hard timeout, twice |
| `2951f80` + correction | success, 40s, tools 7–15s; reasoning, visualizations, table usage and event→fetch checks all intact |

Correction: in single-writer mode the callback reads and writes through
`self.db` under `_tool_db_lock` (the pre-fix behaviour); off single-writer it
keeps its own short-lived session. Postgres re-verified with the Loop A repro
(all modes as above) and a real parallel run.

Regression test: `test_single_writer_reasoning_stream_uses_the_writer_session`
holds SQLite's write lock on the agent session and fails if the stream opens a
second session — **fails on `2951f80` (2 of 2), passes with the correction.**
