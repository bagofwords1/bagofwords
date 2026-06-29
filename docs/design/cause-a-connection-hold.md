# Cause A — request-scoped DB connection held for the whole request (design, not yet implemented)

## Problem

Every authenticated request checks out **one** pooled DB connection and holds it
for its **entire lifetime**. `get_async_db` (`backend/app/dependencies.py:48`) is
an `async with async_session_maker()` generator: it acquires a connection on
first use and FastAPI only tears it down *after the response is fully sent*. The
handler + `current_user` + `get_current_organization` share that one session
(FastAPI caches the dependency), so it's one connection per in-flight request.

Because these are read GETs that never commit, the connection sits **idle in
transaction** between queries and across response serialization. Under a burst,
the pool (`pool_size 20 + max_overflow 20` = 40/worker, `pool_timeout 30`) fills
and further requests wait up to 30s, then 500 with `QueuePool ... timed out`.

Reproduced (local Postgres, 1 worker): the latency knee sits exactly at the pool
size — 30 concurrent OK, 60 collapses with 500s — and it did **not** move when we
made the endpoints fast (PR #482). It's a property of *connection-hold-time ×
concurrency*, not per-query cost.

This is **distinct** from the capacity question (raising `pool_size` /
`max_connections`). This doc is only about the *hold model*.

## There is already a precedent in the codebase

`completion_service.create_completion_stream`
(`backend/app/services/completion_service.py:2199-2204`) explicitly does:

```python
await db.commit()
await db.close()   # release the request-scoped connection before streaming
```

with a comment that keeping it open "leaves it `idle in transaction` and starves
the pool, which is exactly how `get_current_organization` started timing out at
30s under concurrent load." The SSE path learned this lesson; the rest of the app
hasn't. `get_async_db`'s `finally` already tolerates a handler that closed the
session early (it swallows the rollback). So **early release is already a
supported pattern** — we just don't use it outside SSE.

## Constraints

- **Blast radius.** `Depends(get_async_db)` appears at **416 call sites across
  108 route files**. Anything that rewrites the dependency model touches
  everything → not acceptable as a first step.
- **Serialization must stay safe.** If we release the session before FastAPI
  serializes the `response_model`, the returned object must be detached-safe
  (a Pydantic model or fully-materialized data, not lazy ORM objects). The heavy
  read endpoints already return Pydantic (`ReportListResponse`, the instructions
  list dict), so they're safe; arbitrary endpoints returning ORM objects are not.

## Options

### Option 1 — Targeted early-release in the hot read endpoints (low risk)
Generalize the SSE pattern with a tiny helper and call it in the handful of
heavy list endpoints right after the Pydantic response is built, before
returning:

```python
async def release_request_db(db: AsyncSession) -> None:
    try: await db.commit()
    except Exception: pass
    try: await db.close()    # returns the connection to the pool now
    except Exception: pass
```

- Pros: proven pattern, ~5–10 endpoints, no global behavior change, immediately
  shortens hold time for the worst offenders (reports/instructions lists fired on
  every page). `get_async_db`'s finally already handles the early close.
- Cons: per-endpoint and easy to forget; doesn't help the other ~400 sites.
- Verifiable now with `backend/scripts/concurrency_bench.py`.

### Option 2 — Auto-release for GET endpoints via a custom APIRoute (systemic, medium risk)
A custom `APIRoute`/router that, for GETs, commits+closes the request session
**immediately after the handler returns its value**. Apply it router-by-router
(opt-in) so the blast radius is bounded.

- Pros: covers a whole router at once without editing each handler.
- Cons: FastAPI serializes inside the route handler, so interposing "after handler
  value, before serialization" is awkward — needs care to release at the right
  point and to guarantee handlers under that router return detached-safe values.
  Requires an audit of each router's return types before opting it in.

### Option 3 — Session-as-scope inside handlers (the "correct" refactor, large)
Replace `Depends(get_async_db)` with explicit `async with session_scope() as db:`
blocks: do DB work, build the Pydantic response inside the block, exit (connection
released), return. This is the clean architecture and also fixes
"connection held idle across `await`s between queries."

- Pros: the real fix; connection held only while actually needed.
- Cons: 416 call sites → very large, high risk. Not a near-term step.

### Option 4 — PgBouncer (transaction pooling) in front of Postgres (infra)
Put PgBouncer between app and PG in transaction-pooling mode; point the app's
(large) pool at PgBouncer. PgBouncer multiplexes many app connections onto few
real PG backends, **but only returns a backend to its pool when the transaction
ends**. So it only helps once our requests stop holding a transaction open for
the whole request (i.e. after short-transaction reads). With asyncpg, requires
`statement_cache_size=0` and disabling prepared statements.

- Pros: the production-grade answer to "many app connections, few DB slots";
  lets the app scale to hundreds of concurrent requests.
- Cons: new infra component; only effective combined with short transactions
  (Options 1–3); asyncpg caveats.

### Option 5 — End the read transaction promptly (complement, not a standalone fix)
Commit/rollback right after each read so the connection is `idle` rather than
`idle in transaction`. Reduces PG-side lock/vacuum pressure and is the
*precondition* that makes Option 4 effective — but does **not** free the pool
slot on its own (the session is still checked out until close).

## Decision (updated)

- **Phase 1 — IMPLEMENTED** (this PR). Targeted early connection release on the
  hot read endpoints.
- **Phase 2 — DROPPED** after investigation (see "Phase 2 — dropped" below). The
  separate AUTOCOMMIT read session would *double* connections per request and
  app-wide autocommit would break write atomicity; the remaining safe benefit is
  marginal once Phase 1 is in place. Not worth the risk.
- **Next lever for the production collapse:** the `max_connections` / pool
  capacity item (out of scope here) — raising Postgres `max_connections` so
  `workers × pool_size` fits. That is what actually lets a ~200-request page
  burst through; Phase 1 only shortens how long each request holds its slot.
- Option 3 (full 416-site refactor) and Option 4 (PgBouncer) remain out of scope.

> Measurement note: the local single-worker sandbox is too noisy in the
> contended regime to produce a clean before/after (the same config flips between
> "all 200 OK" and pool-timeout collapse across trials, dominated by cold-start
> and shared-CPU variance). Phase 1 is justified by the mechanism (frees the
> pooled connection before serialization) and by mirroring the already-proven SSE
> early-release in `completion_service`, not by a sandbox throughput delta.

---

# Phase 1 — Targeted early connection release (IMPLEMENTED)

**Goal:** free the pooled connection back to the pool *before* response
serialization on the highest-frequency read endpoints, so a burst of those calls
stops pinning the pool for each request's full wall-time.

**Mechanism.** Add one helper and call it at the end of each target handler,
after the Pydantic response object is built and before `return`:

```python
# app/dependencies.py (or a small app/db_utils.py)
async def release_request_db(db: AsyncSession) -> None:
    """Return the request's pooled connection to the pool now, instead of at
    response-send. Safe only when the caller will not touch `db` again and the
    return value is detached-safe (Pydantic / plain data). Mirrors the SSE path
    in completion_service.create_completion_stream."""
    try:
        await db.commit()      # end the read txn; commit is a no-op for pure reads
    except Exception:
        pass
    try:
        await db.close()       # <-- returns the connection to the pool
    except Exception:
        pass
```

`get_async_db`'s `finally` already tolerates a pre-closed session (its rollback is
wrapped in try/except), so no dependency change is needed.

**Target endpoints (per-page, highest frequency — all return detached-safe Pydantic):**
| endpoint | handler | returns |
|---|---|---|
| `GET /reports?view=minimal` (sidebar, every page) | `report.py:78` | `ReportListResponse` |
| `GET /reports` (full list) | `report.py:78` | `ReportListResponse` |
| `GET /instructions` | `instruction.py:78` | dict of `InstructionListSchema` |
| `GET /instructions/pending-changes` | `instruction.py:312` | `{"instruction_ids": [...]}` |
| `GET /data_sources/active` | `data_source.py:47` | `list[DataSourceListItemSchema]` |
| `GET /data_sources/{id}/full_schema` | `data_source.py:163` | `PaginatedTablesResponse` |

Call site shape (service-returns-schema endpoints): build the result, then
`await release_request_db(db)` immediately before returning it.

**Guardrails / preconditions (verify per endpoint before adding the call):**
- The handler must not use `db` after the release (no post-serialization DB work,
  no background task that reuses the request session).
- The returned value must be fully materialized (Pydantic/dict), not lazy ORM
  objects — confirmed for all six above.
- `current_user` (which writes `last_seen`) and `get_current_organization` run
  *before* the handler body, so they're done by the time we release.

**Blast radius:** 6 endpoints + 1 helper. No global behavior change.

**Verification:** `scripts/concurrency_bench.py` against the seeded Postgres
harness — expect the latency knee to move up (more concurrency before the
`QueuePool ... timed out` 500s) and p50 under burst to drop. Add a before/after
table to the PR.

**Effort:** ~½ day incl. measurement.

---

# Phase 2 — Read-only autocommit session (DROPPED)

> **Dropped after investigation.** Two architectural blockers make the scoped
> approach unsafe here, and the residual benefit is marginal:
>
> 1. **Doubles connections per request.** `current_user` (`auth.py:931`) and
>    `get_current_organization` (`dependencies.py:92`) both `Depends(get_async_db)`.
>    A handler using a *separate* `get_read_db` session would hold a 2nd pooled
>    connection for the request — a regression that worsens pool pressure.
> 2. **App-wide AUTOCOMMIT breaks writes.** Write services rely on implicit
>    multi-statement transactions (`db.add(...)` ×N then `db.commit()`), so making
>    the shared session autocommit would lose write atomicity.
> 3. **Marginal residual benefit.** Phase 1 already early-releases the *heavy*
>    holds; the remaining page-load GETs are cheap/short. And the idle-in-tx /
>    `last_seen`-lock rationale is already handled — `_update_last_seen` commits
>    immediately and the sampler showed `lockwait=0`.
>
> Net: no separate read-session mechanism is worth building. If an individually
> heavy read endpoint shows up hot later, apply the Phase 1 `release_request_db`
> one-liner to it (that's "more Phase 1", not a new mechanism). The real remaining
> lever for the production burst is the capacity item.
>
> Original Phase 2 sketch retained below for context.

## (original sketch — not implemented)

**Goal:** generalize Phase 1 to *all* read endpoints and additionally stop reads
from sitting **idle-in-transaction** (which blocks VACUUM, holds locks, and is the
mechanism behind the `last_seen` row-lock amplifier).

**Mechanism.** A dedicated read dependency + a read router that auto-releases:

```python
# AUTOCOMMIT: each SELECT is its own statement, no lingering transaction.
read_session_maker = create_async_session_factory(
    bind=engine.execution_options(isolation_level="AUTOCOMMIT")
)

async def get_read_db() -> AsyncGenerator[AsyncSession, None]:
    async with read_session_maker() as session:
        yield session
```

Plus a small custom `APIRoute` (applied to read-only routers) that closes the
request's read session right after the handler returns its value — so the
auto-release of Phase 1 happens for every endpoint on that router without
per-handler calls.

**Scope of application (opt-in, ordered):**
1. The same 6 hot endpoints from Phase 1 (swap `get_async_db` → `get_read_db`;
   the early-release is then automatic via the read router).
2. Then broaden to other **GET** endpoints router-by-router after a quick audit.

**Hard guardrails:**
- **GET / pure-read only.** AUTOCOMMIT removes multi-statement atomicity, so it
  must never be used on POST/PUT/DELETE or any handler that writes as a unit.
- **`_update_last_seen` interaction.** `current_user` writes `last_seen` on the
  request session. Under AUTOCOMMIT that single UPDATE still commits correctly
  (it's standalone), so it's compatible — but this must be re-confirmed when
  `current_user` shares the read session, and is a reason to keep write endpoints
  on the existing `get_async_db`.
- Endpoints that read-then-write in one handler stay on `get_async_db`.

**What Phase 2 does and does not do (be honest):**
- ✅ Removes idle-in-transaction for reads (VACUUM/lock/`last_seen`-contention
  relief) and, via the read router's auto-close, frees the pool slot for *all*
  migrated GETs (not just the 6 in Phase 1).
- ❌ Does not, by itself, raise the absolute pool ceiling — a request still holds
  one slot while actively running. (That ceiling is the out-of-scope capacity item.)

**Blast radius:** new dependency + read router (small), then a gradual per-router
migration of GET endpoints (bounded, reversible).

**Verification:** `concurrency_bench.py` for the knee; plus a Postgres
`pg_stat_activity` sample showing `idle in transaction` → `idle` for read
endpoints under load (the sampler in the contention feedback-loop doc).

**Effort:** ~1–2 days for the mechanism + the hot set; migration of remaining GET
routers incremental thereafter.

## Open question for review
- Phase 2 migration breadth: stop at the hot read endpoints, or commit to
  migrating all GET routers to the read session over time?
