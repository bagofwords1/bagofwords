# Sandbox Feedback Loop — why /agents (and every page) is slow: pool exhaustion + reports cascade

Follow-up to `sandbox-feedback-loop-agents-instructions-perf.md`. After the
instructions `pending-changes` N+1 was batched, production `/agents` was still
showing **~10s on three unrelated endpoints** (`instructions`,
`pending-changes`, `reports?filter=my`). The uniform latency across heterogeneous
endpoints pointed at a *shared* bottleneck, not per-query cost. This doc
reproduces it on real Postgres.

## Two independent, compounding causes (both reproduced)

### Cause B — `GET /reports?filter=my&limit=50` is independently slow (a `selectin` cascade)

- Fired from the **global layout on every page**: `frontend/layouts/default.vue:525`
  (`fetchRecentReports` → sidebar "recent reports"). So this hits *every* page.
- `Report` declares ~13 relationships as `lazy="selectin"`
  (`backend/app/models/report.py:51-68`): `widgets`, `completions`, `queries`,
  `visualizations`, `text_widgets`, `files`, `artifacts`, `scheduled_prompts`,
  `shares`, `stars`, … several recursing (`widgets→steps`,
  `queries→steps→visualizations`, `completions→mentions`). The list query
  (`report_service.py:1235-1243`) does **not** suppress this, so listing 50
  reports hydrates the entire widget/query/step/completion graph for all 50.
- **Measured** (50 reports, ~2000 completions, ~500 steps, on Postgres):
  **2.55s, 46 SQL statements**. Query count is ~constant (batched selectin), so
  it's **row-volume / payload bound** — it grows with conversation length, which
  is why long-conversation prod reports reach ~10s.

### Cause A — connection-pool exhaustion under the page's request storm

- The page fires **~200 requests at once** (many duplicates: `settings`×8,
  `count`, `data_sources`, `ping`).
- Pool is **`pool_size=20 + max_overflow=20` = 40 per worker**,
  `pool_timeout=30` (`backend/app/settings/database.py:248-255`).
- **Each request holds its DB connection for its entire lifetime** —
  `get_async_db` (`backend/app/dependencies.py:48`) yields the session until the
  response is sent; the handler + `current_user` + `get_current_organization`
  share that one connection. Under load the pool fills with connections parked
  **idle-in-transaction**.

## Environment (real Postgres — SQLite can't model pools/locks)

```bash
# Postgres 16 as the unprivileged postgres user (Docker daemon not available here)
PGBIN=/usr/lib/postgresql/16/bin; PGDATA=/var/lib/postgresql/bowpg
mkdir -p $PGDATA && chown postgres:postgres $PGDATA && chmod 700 $PGDATA
runuser -u postgres -- $PGBIN/initdb -D $PGDATA --auth=trust -U postgres
runuser -u postgres -- $PGBIN/pg_ctl -D $PGDATA \
  -o "-p 55432 -c max_connections=200 -c listen_addresses='127.0.0.1'" -l $PGDATA/pg.log start
runuser -u postgres -- $PGBIN/psql -h 127.0.0.1 -p 55432 -U postgres -c "create database bow;"

cd backend
export BOW_DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:55432/bow"
export BOW_SMTP_PASSWORD=dummy ANTHROPIC_API_KEY=dummy
uv run alembic upgrade head
uv run python main.py &     # backend :8000 (database.py rewrites to +asyncpg)
# signup sandbox@bow.dev, capture token+org into /tmp/token.txt /tmp/org.txt
```

Seed:
```bash
SEED_COMPLETIONS=40 SEED_WIDGETS=5 SEED_BLOB=120 \
  uv run python scripts/seed_reports_cascade.py 50          # heavy reports (Cause B)
uv run python scripts/seed_instructions_pending.py 300 1.0  # instructions + pending builds
```

## Loop — measurements

```bash
uv run python scripts/profile_reports_list.py        # Cause B: query count + time, isolated
uv run python scripts/concurrency_bench.py 150       # Cause A: burst (mix of the 3 endpoints)
uv run python scripts/concurrency_bench.py 60        # finds the knee
```

### Observed

Single request, isolated (warm):

| endpoint | time |
|---|---|
| `reports?filter=my&limit=50` | **1.9 s** (cascade, Cause B) |
| `instructions?limit=200` | 0.7 s |
| `instructions/pending-changes` | **0.17 s** (the shipped fix holds) |

Concurrency sweep (MIX of the 3 endpoints), pool = 40:

| concurrency | p50 | max | status |
|---|---|---|---|
| 10 | 3.7 s | 6.8 s | 200×10 ✅ |
| 30 | 8.1 s | 17.7 s | 200×30 ✅ (degrading) |
| 60 | 60 s | 60 s | **20×500, 40×timeout** ❌ |
| 150 | 31 s | 60 s | **110×500, 40×timeout** ❌ |

Postgres during the 150 burst: `active=1  idle_tx=40  lockwait=0  total=42` — the
pool is pinned at 40, **all 40 connections idle-in-transaction**, only 1 query
running. Backend log: `sqlalchemy.exc.TimeoutError: QueuePool limit of size 20
overflow 20 reached, connection timed out, timeout 30.00` ×299.

**The knee is exactly at the pool size.** Even the *fast* endpoint
(`pending-changes`, 0.17s isolated) collapses to p50 30s + 110×500 at concurrency
150 — proving the failure is the pool/concurrency model, not per-query cost.

## Conclusions

1. **The instructions N+1 fix was necessary but not sufficient.** `pending-changes`
   is now 0.17s isolated, but the page is still slow because the wall-clock is
   gated by (A) pool exhaustion from the ~200-request burst and (B) the reports
   cascade fired on every page.
2. **Cause A is the dominant, shared cause** — it explains the uniform ~10s and
   the 500s, and it hits every endpoint once concurrency exceeds the 40-connection
   pool. Each request holding its connection for its full lifetime is the core
   issue.
3. **Cause B amplifies A** (slow reports requests hold connections ~2s each) and
   is independently slow on every page via `default.vue`.
4. **Not observed:** the `_update_last_seen` row-lock contention hypothesis —
   `lockwait` stayed 0 in these runs (requests serialize on the event loop before
   they contend on the row). It remains a plausible secondary amplifier but was
   not the dominant factor here.

## Candidate fixes (not implemented — investigation only)

- Cause B: a lightweight list projection / `raiseload("*")` for the reports list so
  it stops hydrating the widget/query/step/completion graph; serve only what the
  sidebar shows.
- Cause A: raise `pool_size`×workers to comfortably exceed the burst; release the
  DB session earlier (don't hold a connection across response serialization);
  and cut the frontend request storm (dedupe `settings`/`count`/`ping`, lazy-load).

## Artifacts

- `backend/scripts/seed_reports_cascade.py` — reports + deep object graph
- `backend/scripts/profile_reports_list.py` — reports list query count + time
- `backend/scripts/concurrency_bench.py` — concurrent burst latency distribution
- `backend/scripts/seed_instructions_pending.py` — now reads BOW_DATABASE_URL (PG/SQLite)
