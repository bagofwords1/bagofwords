# Feedback loop — parallel tools queued on a lock they didn't need (Postgres)

Parallel `create_data` calls finished one after another, about 13s apart. On
Postgres, `_tool_db_lock` was held around `_handle_tool_output` and
`_handle_streaming_event`, which only matters when writes share `self.db`. Off
single-writer mode (the Postgres default), each of those writers opens its own
short-lived session through `_writes_session()`, so the lock made tools queue
behind each other's persistence without protecting anything.

## Change (`backend/app/ai/agent_v2.py`)

- `_single_writer_guard()` returns `_tool_db_lock` in single-writer mode
  (always on SQLite) and `nullcontext()` otherwise. It is used around
  `_handle_streaming_event` and `_handle_tool_output`. The post-tool context
  refresh and the rest of that section still hold `_tool_db_lock`, because
  they touch `self.db`.
- Table-usage analytics (`emit_table_usage` + `tables_by_source`):
  - Off single-writer, they run on a background session through
    `_schedule_bg_write`, which `_drain_bg_writes` drains before
    `completion.finished`. They are scheduled after `update_step_status` has
    committed, so the background read sees the final step.
  - In single-writer mode they stay inline, as before.
  - No UI event depends on these rows.

SQLite behaviour is unchanged: same lock, same inline writes.

## Loop — real scenarios, before vs after

The sandbox ran Postgres 16 behind `tools/agent/pg_latency_proxy.py` (15ms one
way), with Claude Haiku 4.5 and the Music Store demo data, driven through the
chat UI with Playwright.

Each run was verified from:
- DB rows: tools, steps (rows through `GET /api/steps/{id}`), visualizations,
  default steps, and table-usage events against the recordable tables;
- the live SSE stream, including an event→fetch check: on every
  `tool.finished` / `visualization.updated`, fetch the object the way the UI
  does and compare it with the final committed state;
- the UI after a reload;
- backend-log warnings and errors;
- an instrumented `_tool_db_lock` (local only, not committed).

| scenario | before | after |
|---|---|---|
| 3 tables in parallel (3 runs each): parallel batch | 82 / 76 / 76s | 44 / 47 / 37s |
| same: whole run | 117 / 108 / 111s | 77 / 81 / 72s |
| 3 tables in parallel, data source attached (2 each): batch | 94 / 94s | 48 / 41s |
| same: whole run | 134 / 129s | 80 / 79s |
| lock held / waited per three-tool run | 61–82s / 72–96s | 6–7s / 7–9s |
| single chart (2 each): whole run | 100 / 69s | 100 / 63s (no parallel tools, so no change expected) |

Every run on both sides passed all of these checks:
- tools, steps, visualizations and default steps all `success`;
- **0 event→fetch mismatches** (36 before, 36 after);
- table usage equal to what was recordable (8/8, 7/7, 7/7, 7/7 wherever
  recordable);
- no new log errors.

Two things were identical on both sides:
- Duplicate SSE sequence numbers exist before and after. They come from the
  handlers' fresh-copy `next_seq(fresh_db, exec_obj)`; every
  `block.delta.artifact` / `query.created` / `visualization.*` event is
  affected, in the same proportion on both sides. This is a pre-existing
  issue, not caused by this change.
- The `inspect_data` table-usage warning (`step=None`) is also pre-existing.

SQLite: same parallel scenario after the change, success in 40s with all
checks intact (see `coder-reasoning-missing-greenlet.md` for the SQLite
regression found and fixed while running this loop).

Caveat: the gain scales with DB round-trip cost and the number of parallel
tools. With a database on the same host it will be smaller. Single-tool turns
see none.
