# Single-writer agent loop: design for the next contention pass

Status: **proposed** (follow-up to the prompting-fix branch
`claude/improve-dashboard-prompting-EW8av`).

## Problem

The agent loop currently has **5+ concurrent DB writers** per iteration on
independent SQLAlchemy sessions:

1. `self.db` — main session for plan_decision save, completion_block upsert,
   etc. (long-lived).
2. `_handle_streaming_event` — per-progress-event `fresh_db` (still active for
   `create_widget`, `describe_entity`, `write_csv`; deferred for `create_data`
   after Path B at `63ef403`).
3. `_handle_tool_output` — per-tool `fresh_db`. Path B added query/step/viz
   creation here.
4. `_bg_persist_tool` — bg session via `_session_maker`. Inserts
   `tool_executions` + updates `completion_blocks`.
5. `rebuild_completion_from_blocks` — bg session. Re-renders the completion's
   `content` field from blocks.
6. Multiple agent-level bg tasks: token metadata, instruction usage, snapshot
   saves (initial + pre_tool + post_tool). Now tracked via
   `_schedule_bg_write` after `8f1dc12`, but still independent sessions.

Under SQLite (test/dev/eval), all writers contend for the WAL writer lock.
The retry-on-lock helper at `project_manager.py:112` (1+2+4s backoff,
`_DB_COMMIT_TIMEOUT_S=35s` per attempt) can stretch a single rebuild to
~150s under bad contention. Drain-before-deferred-create coordinates some of
this, but the failure mode is now: rebuild's commit fails fast → my INSERT's
commit also fails fast → both queue up → silent state corruption
(`self.current_visualization=None` in the old path; failed entity creation
in the Path B path).

Postgres avoids most of this through row-level locks, but the same
architectural race exists. The user reports the original screenshot bug
manifested on PG too — likely the rare PG variant of this same issue.

## Why incremental fixes converged but didn't close

The fixes shipped on the prompting branch (`63ef403` through `036cfee`) all
helped — eval rules 2 and 3 went from FAIL to PASS, wall time dropped from
634s → ~340s — but they're working around the architecture rather than
fixing it. Each layer peeled one onion ring; the next ring shows up. The
core problem is structural: too many concurrent writers on too many sessions.

Concretely, on the latest branch state (`036cfee`):

```
SQLite eval: 3/4 rules pass, judge fails
Postgres eval: 4/4 rules pass (validated via curl/sandbox)
```

We can ship more SQLite-targeted fixes, but they all run into the same wall:
WAL serializes writers, and we have too many of them.

## Proposed architecture

**One dedicated write session per agent run, single-writer, sequential.**

```
                         Agent run lifecycle
                                │
                                ▼
              ┌────────────────────────────────────┐
              │     self._writes: AsyncSession     │  ← opened at run start,
              │  (the agent's only writer session) │     closed at completion
              └────────────────────────────────────┘
                                │
       ┌────────────────────────┼─────────────────────────┐
       ▼                        ▼                         ▼
   plan_decision        tool_execution             entity creation
   completion_block      INSERT/UPDATE            (query/step/viz)
   upserts                                        on tool_output
       │                        │                         │
       └─────── all use self._writes, sequentially ───────┘
                                │
                                ▼
                  Single SQLite WAL writer at a time
                       No concurrency, no race
```

### Concrete shape

```python
class AgentV2:
    async def run(self, ...):
        async with self._session_maker() as writes:
            self._writes = writes
            try:
                # ... agent loop body ...
                # ALL DB writes go through self._writes via project_manager
                # helpers refactored to take an `AsyncSession` parameter.
            finally:
                await writes.close()
                self._writes = None
```

Reads (e.g. `context_hub.refresh_warm`) keep using their own short-lived
sessions or the main `self.db` — those don't contend on writes in WAL mode.

### What goes through `self._writes`

| Currently on... | Moves to `self._writes` |
|---|---|
| `self.db` for plan_decision/block upsert | yes |
| `_handle_streaming_event` `fresh_db` | yes (or removed if streaming path is fully deferred) |
| `_handle_tool_output` `fresh_db` | yes |
| `_bg_persist_tool` bg session | yes — inline as sync write, no longer "bg" |
| `rebuild_completion_from_blocks` bg session | yes — sync at end of completion, no longer per-iteration |
| Snapshot saves bg sessions | yes |
| Token metadata / instruction usage bg sessions | yes |

Reads stay independent. The `self.db` session can still exist for the
agent's main coroutine reads but it won't write anymore.

### What this gives up vs. today

- **Bg-write parallelism with the planner**: today the planner LLM call
  starts before tool_execution INSERT lands. After this refactor, the
  INSERT happens sync inline before the next iteration. Cost: maybe
  100–200ms per iteration on PG, more on SQLite. Worth it for determinism.
- **Mid-streaming widget UI updates**: streaming events that wrote
  query/step/viz to make widgets appear early in the UI. Path B already
  removed this for `create_data`. Generalizing to all streaming tools
  means widgets only appear at `tool.end`. Acceptable; the SSE flow can
  emit "widget loading" sentinels mid-stream without DB writes.

## Migration plan

### Phase 1 — Plumbing

1. Introduce `self._writes` as an optional dedicated write session opened
   at the start of `run()`, closed in the finally block.
2. Audit `project_manager` helpers: every helper that currently takes
   `db: AsyncSession` and writes should still work with `self._writes`.
   No API change needed — they already accept any session.
3. Add a feature flag `BOW_AGENT_SINGLE_WRITE_SESSION=true` that gates the
   new behavior. Default off; enable in CI/eval first.

### Phase 2 — Convert writers one at a time

In order of impact:

1. **`_bg_persist_tool` → sync inline write.** Replace
   `_schedule_bg_write("persist_tool", ...)` with a direct
   `await self.project_manager.commit_tool_and_attach_block(self._writes, ...)`.
2. **`rebuild_completion_from_blocks` → sync at completion.finished only.**
   Already deferred from per-iteration in `036cfee`. Make it a sync call
   on `self._writes` at completion.finished, awaited before the SSE event.
3. **Snapshot saves** (initial / pre_tool / post_tool) → sync writes on
   `self._writes`. They're already lightweight.
4. **Token metadata / instruction usage** → sync writes on `self._writes`.
5. **`_handle_tool_output`'s entity creation (Path B)** → use
   `self._writes` instead of opening `fresh_db`.
6. **`_handle_streaming_event`'s entity creation (other tools)** → defer
   to `_handle_tool_output` (mirror Path B for `create_widget`,
   `describe_entity`, `write_csv`).

### Phase 3 — Remove the feature flag

Once all writers are converted and CI is green on both SQLite and Postgres
for one full sprint, remove the flag and delete the legacy paths.

## Validation

1. Unblock the `dashboard_reuse` eval on SQLite (currently PG-only).
2. Run the full eval matrix on both backends; expect deterministic results
   (same input → same DB state).
3. Microbenchmark: agent run wall time on a representative case before vs.
   after. Tolerate up to 10% regression for the determinism win.
4. Soak test: 100 sequential agent runs on SQLite, expect zero
   `database is locked` warnings.

## Risks

- **Long-held write transaction.** A single session held for the full
  agent run is unusual. PG's `idle_in_transaction_session_timeout` (already
  set to 60s in test config) would terminate it. Mitigation: commit
  periodically inside the loop (per-iteration), not just at the end. Each
  iteration is a unit of work; commit completes that unit.
- **Error recovery.** If `self._writes` enters `PendingRollback` mid-run,
  every subsequent write fails. Mitigation: explicit rollback handlers at
  iteration boundaries; on persistent failure, abort the run with a
  structured `agent.persist_error` SSE.
- **Backwards compatibility.** External tools (MCP, knowledge harness)
  that interact with the agent's session may need to open their own
  sessions. Audit `tools/mcp/*.py` and `agents/*.py` for `self.db`
  usage that needs splitting reads vs writes.

## Effort estimate

| Phase | Effort | Risk |
|---|---|---|
| Phase 1 (plumbing + flag) | 1 day | Low |
| Phase 2.1 (`_bg_persist_tool`) | 0.5 day | Low |
| Phase 2.2 (rebuild) | 0.5 day | Low |
| Phase 2.3 (snapshots) | 0.5 day | Low |
| Phase 2.4 (token/instruction) | 0.5 day | Low |
| Phase 2.5 (`_handle_tool_output` Path B sites) | 1 day | Medium |
| Phase 2.6 (`_handle_streaming_event` for other tools) | 1.5 days | Medium |
| Validation + bench + soak | 1 day | Low |
| Phase 3 (flag removal) | 0.5 day after soak | Low |
| **Total** | **~6.5 days** | **Medium overall** |

## What ships now (this branch)

The prompting fix is the user-visible win and shipped via PR-A. The Path B
+ follow-on commits on this branch (`63ef403` through `036cfee`) reduce
SQLite contention substantially without fully closing it. They're correct
improvements either way and worth keeping.

The `sanity_dashboard_reuse.yaml` eval case is currently PG-passing,
SQLite-failing. Tag it `requires_postgres` so SQLite CI doesn't fail on a
known-flaky case until this refactor lands.

## Open questions to resolve before kickoff

- **Feature flag scope**: per-organization, per-environment, or process-wide?
- **Mid-streaming UI updates**: do we keep them via SSE-only (no DB writes)
  or accept the UX regression of widgets-at-tool-end-only?
- **Knowledge harness path**: it has its own loop pattern. Does it inherit
  the single-writer model or stay on its current sessions?

These don't block design; they're scope choices for kickoff.
