# Sandbox Feedback Loop — silent context loss in the agent loop, and what actually drains the connection pool

Investigated from a customer report on self-hosted Kubernetes with PostgreSQL:
runs failing with

```
Agent failed: remaining connection slots are reserved for roles with the SUPERUSER attribute
```

and, on a later turn, the same failure wearing a different mask:

```
Agent failed: This Session's transaction has been rolled back due to a previous
exception during flush. ... Original exception was: remaining connection slots
are reserved ...
```

The customer's other complaint, on the same report, was that the agent kept
losing the thread — "the parameters aren't dependent on each other, and the
period doesn't update". That turned out to be the same investigation.

## Environment

Real PostgreSQL, because SQLite models neither pools nor the session semantics
at issue. Sized to the customer's exact shape (stock server defaults).

```bash
PGBIN=/usr/lib/postgresql/16/bin; PGDATA=/var/lib/postgresql/bowpg
runuser -u postgres -- $PGBIN/initdb -D $PGDATA --auth=trust -U postgres
runuser -u postgres -- $PGBIN/pg_ctl -D $PGDATA \
  -o "-p 55432 -c max_connections=100 -c listen_addresses='127.0.0.1'" -l $PGDATA/pg.log start
runuser -u postgres -- psql -h 127.0.0.1 -p 55432 -U postgres -c "create database bow;"

cd backend
export BOW_DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:55432/bow"
export BOW_ENCRYPTION_KEY=<fernet> ENVIRONMENT=production
uv run alembic upgrade head && uv run python main.py
```

`max_connections=100`, `superuser_reserved_connections=3` → **97 usable**.
Seeded via the API: user + org, an Anthropic provider with *only* Claude 4.5
Haiku enabled (so it is both default and small-default), and a `postgresql` data
source pointing at a second database with a 4 000-row `services` table. Agent
turns were driven through `POST /api/reports/{id}/completions` with
`stream: true`, i.e. the real SSE path.

## Finding 1 — `refresh_warm` raced on the agent's session, and lost context silently

`ContextHub.refresh_warm` ran its four warm builders under
`asyncio.gather(..., return_exceptions=True)`. All four are constructed with the
**same** `AsyncSession` (`context_hub.py:352-356`, fed `self.db` from
`agent_v2.py:754`). An `AsyncSession` is not safe for concurrent use, so the
overlapping calls raised:

```
ERROR app.ai.context.builders.query_context_builder:
Failed to load queries for report ...: This session is provisioning a new
connection; concurrent operations are not permitted
```

This fired on the **first live turn**. `prime_static` had the identical shape,
with higher stakes — a lost section there is the schema or the instructions.

### The trigger is the pool-release optimisation

An isolated probe showed **0 errors**, which was the clue. The builders only
collide while the session is *provisioning* a connection — and the widest such
window is when the session holds no connection at all. That is exactly the state
`agent_v2._release_db_between_steps()` leaves it in: it commits before each loop
iteration, which returns the connection to the pool. Modelling that in the probe
made it deterministic:

| probe (20 rounds) | racing `gather` | serialized |
|---|---|---|
| swallowed concurrency errors | **40** | **0** |
| `queries` section empty | **20/20** | **0/20** |
| `entities` section `None` | **20/20** | **0/20** |
| `refresh_warm` wall-clock | 14 ms | 13 ms |

### The damage was invisible

Each builder catches its own failure and returns an empty section, so nothing
propagated. On a report with two saved queries:

| | before | after |
|---|---|---|
| queries in the database | 2 | 2 |
| **queries the planner actually sees** | **0**, in 10/10 refreshes | **2**, in 10/10 |

The `<queries>` section is the agent's record of what it has already run. A user
refining a previous answer — "now break that down by month" — was talking to an
agent that had forgotten the thing it was refining. That is the "parameters
aren't dependent on each other" complaint, and it is not a prompting problem.

### There was never any parallelism to lose

Four builders sharing one session share one DBAPI connection, so their queries
could not overlap on the wire even in principle.

Measured directly, interleaving the two versions over three repetitions of 40
refreshes each, on a warm session where neither version races (so both produce
the same two populated sections):

| `refresh_warm` p50 | rep 1 | rep 2 | rep 3 |
|---|---|---|---|
| `gather` | 14.39 ms | 14.26 ms | 14.38 ms |
| serialized | 14.55 ms | 13.94 ms | 14.09 ms |

No measurable cost — the serialized version is marginally *ahead* on two of
three reps, well inside run-to-run noise.

On the cold path (a session that has just committed, i.e. the real loop path)
the raw numbers do differ, and the comparison is not like-for-like:

| cold session | p50 | sections populated |
|---|---|---|
| `gather` | 11.21 ms | **1 of 4** |
| serialized | 14.30 ms | **2 of 4** |

The racing version is ~3 ms faster *because it is failing*: a builder that loses
the race returns an empty section immediately instead of running its query. The
extra 3 ms is the cost of actually doing the work that used to be silently
dropped, not overhead introduced by the lock.

A caveat on end-to-end numbers: agent wall-clock across sandbox runs ranged
29–88 s for the *same* context code, tracking LLM call volume (58 vs 107 calls
between two 10-agent runs). End-to-end timings here cannot resolve a few ms per
refresh, which is why the isolated benchmark above is the one to trust.

Under load it got worse, as the window widens: a 10-agent concurrent run logged
**26 swallowed concurrency errors across 98 refreshes**, versus 2 across 15 on a
single-agent run. (Errors, not refreshes — a single refresh can lose more than
one section, so this bounds the rate rather than stating it.)

## Finding 2 — nothing was draining the connection pool

The headline error is *not* a `QueuePool` timeout. It is the server refusing the
connect, which reaches the user as a failed run. The arithmetic:
`(pool_size + max_overflow) × workers × replicas`, where `pool_size` and
`max_overflow` were hardcoded at 20 each and `start.sh` runs up to 4 workers —
160 against 97 usable.

Worker count is itself accidental: the Helm chart sets `resources.requests.cpu`
but no `limits.cpu`, so `start.sh`'s cgroup detection finds no quota and falls
through to `nproc`, which reports the **node's** cores, not the request.

### `pool_use_lifo` alone does nothing — that hypothesis was wrong

The initial theory was that FIFO checkout keeps every slot recently-used so
`pool_recycle` never retires any. Measured, both settle at the pool size and
stay there:

| 20-conn pool, after a 40-way burst, light traffic | open backends over 24 s |
|---|---|
| FIFO | 20, 20, 20, … 20 |
| LIFO | 20, 20, 20, … 20 |

`pool_recycle` is applied lazily at checkout and *replaces* a stale connection
rather than closing it, and SQLAlchemy has no idle-pool reaper. **A QueuePool
never shrinks.**

### What actually drains it

Letting the *server* close idle sessions. With `idle_session_timeout` set and
`pool_pre_ping` (already enabled) reconnecting transparently:

| 20-conn pool, `idle_session_timeout=5s`, 1 query/sec | settles at |
|---|---|
| FIFO | **4** open backends |
| LIFO | **1** open backend |

So `pool_use_lifo` is real but secondary: it multiplies what the timeout can
reclaim, by concentrating traffic so the tail goes genuinely idle.

**`idle_session_timeout` is PostgreSQL 14+, and an unrecognised GUC passed as a
startup parameter makes every connection fail** (verified —
`UndefinedObjectError`). It is therefore off by default and opt-in via
`BOW_DB_IDLE_SESSION_TIMEOUT_MS`; the Helm chart, which bundles PostgreSQL 17,
sets it.

## Finding 3 — single-writer mode on Postgres is harmful (do not enable it)

`_use_single_write_session()` is always on for SQLite and opt-in elsewhere. The
plausible theory was that enabling it on Postgres would cut connection demand,
since every background write otherwise opens its own pooled session. Measured
across two 10-agent runs, it does the opposite:

| 10 concurrent agents | legacy | single-writer |
|---|---|---|
| `got result for unknown protocol state` | **0** | **2**, then **4** |
| `Exception terminating connection` | **0** | **2**, then **4** |
| peak connections | 24 | 25 — *no gain* |
| slowest agent | 61.6 s | 72.7 s |

Funnelling every write through one `AsyncSession` while tool coroutines run
concurrently corrupts the asyncpg connection. `_tool_db_lock` does not cover all
of it. **`BOW_AGENT_SINGLE_WRITE_SESSION` must stay off on Postgres** until that
is addressed; the win it was supposed to deliver is not there.

(The code comment points at `docs/design/single-writer-agent-refactor.md`, which
does not exist — the rollout rationale is lost.)

## Finding 4 — `_mlog` was an out-of-scope name, inverting two error messages

Found while benchmarking. `_mlog` was a closure inside `main_execution`, but two
call sites outside that frame referenced it. Both sit inside `except Exception`
blocks, so the `NameError` was caught and relabelled as the failure of whatever
the block guarded — **on the success path**:

- `_persist_focus_on_use` logged `focus-on-use: commit failed` *after the commit
  had succeeded* (8 times in one 10-agent run), then ran a no-op rollback;
- `_ensure_clients_for_attached` logged `mid-run client construction failed` for
  a client that was built and registered fine.

Both point an operator at the opposite of what happened, and both masked genuine
failures of the same paths.

## Result

All four addressed; final 8-agent concurrent run on the same sandbox:

```
refresh_warm calls:        110      concurrency errors:       0
prime_static calls:          8      sections lost:            0
focus-on-use commit ERR:     0      NameError _mlog:          0
protocol-state errors:       0      pool timeouts:            0
TOTAL ERRORs:                0

connections: max 25, idle after run 2   (was: max 30, idle after run 22)
```

## Still open

1. **`limits.cpu` is unset in the chart**, so worker count follows the node's
   core count rather than the CPU request. Not changed here — it alters the
   resource contract of every existing deployment.
2. **Single-writer on Postgres** (Finding 3) needs the concurrent-session hazard
   fixed before the mode is usable there.
3. **Background writes are unbounded per agent.** `_schedule_bg_write` has no
   concurrency cap and `_pending_writes` is an unbounded list, so
   "connections per agent" is emergent. `BOW_MAX_CONCURRENT_AGENTS` caps agents,
   not connections.
4. **`idle_session_timeout` is opt-in.** Auto-detecting server version at engine
   build would let it default on, at the cost of a startup round-trip.
