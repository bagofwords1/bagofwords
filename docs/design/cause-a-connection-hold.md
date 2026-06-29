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

## Recommended phasing

1. **Phase 1 (do first): Option 1** — early-release helper applied to the
   per-page read endpoints (reports list incl. `view=minimal`, instructions
   list, `pending-changes`, `data_sources/active`, full_schema). Measure the knee
   shift with `concurrency_bench.py`. Low risk, immediate.
2. **Phase 2: Option 5 across read paths** — make GET handlers end their
   transaction promptly (or a read-only session helper), to cut idle-in-tx and
   set up Phase 3.
3. **Phase 3 (strategic): Option 4 (PgBouncer)** — once transactions are short,
   front PG with PgBouncer so a large app pool maps onto a small, safe number of
   PG backends. This is what actually removes the ceiling.
4. **Not planned: Option 3** (full 416-site refactor) unless Phases 1–3 prove
   insufficient. **Out of scope here:** the `pool_size` / `max_connections`
   capacity bump — tracked separately.

## Open questions for review
- Is adding PgBouncer to the docker-compose stack acceptable (one more service),
  or do we prefer to stay app-only (Options 1–2) for now?
- For Phase 1, confirm the target endpoint list and that each returns a
  detached-safe (Pydantic) value before the early release.
