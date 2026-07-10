# Feedback Loop — one agent with 50 connections: `/agents` "takes forever", "the entire system is extremely slow"

Reproduces the reported deployment: **50 connections × ~3,000 tables each all
attached to ONE agent** (~150k `DataSourceTable` rows, only ~100 tables active
across the 50 connections), plus a second unused agent. The `/agents` page
takes forever to load and the whole system degrades. This is a follow-up to
`agents-page-connections-perf.md` / `agents-page-contention.md` — those fixes
are merged and hold (the *list* endpoints stay fast below); the remaining
slowness lives on three different paths that all scale with a single agent's
connection/table volume.

**Status: reproduced, root causes isolated; causes A and B fixed (see "The
fix" below). Cause C (`/schema` Python-side filtering) is documented but not
yet fixed.**

## Root causes (validated)

### A. Opening the agent fires a synchronous live-test of all 50 connections — sequentially, with no timeout

`GET /api/data_sources/{id}` embeds a stale-connection retest sweep:
`DataSourceService.get_data_source` collects every connection whose cached
test is older than `CONNECTION_TEST_TTL_SECONDS = 300`
(`backend/app/services/data_source_service.py:968`) and live-tests each one
**in a sequential loop inside the request**
(`data_source_service.py:1012-1024`). Each iteration runs
`ConnectionService.test_connection`
(`backend/app/services/connection_service.py:666-728`): build a client, dial
the warehouse, and `await db.commit()` — on the request's own session.

Three amplifiers:

- **No connect timeout.** `PostgresqlClient.connect` is
  `sqlalchemy.create_engine(uri).connect()` with no `connect_timeout`
  (`backend/app/data_sources/clients/postgresql_client.py:42-64`), so a
  firewalled/blackholed host waits the OS TCP timeout (~2 minutes) — verified
  below. 50 unreachable connections ≈ **100+ minutes** for one detail request.
- **The sweep re-arms every 5 minutes** (TTL 300s) and fires from more than
  the /agents page: the agents tree fetches the detail on every agent click
  (`frontend/components/KnowledgeExplorer.vue:1156`, via `openAgent`
  at `:1177-1181`) and the report/chat page's agent panel fetches the same
  endpoint (`frontend/components/report/ReportAgentPanel.vue:936`).
- **No dedup across requests.** Two concurrent detail requests each ran their
  own 50-test sweep (measured: 60.4s and 59.6s side by side, tests
  interleaved in the log).

The request holds its pooled DB connection for the sweep's entire wall time
(`get_async_db` yields the session for the request lifetime,
`backend/app/dependencies.py:48-61`; this endpoint never calls
`release_request_db`). On production Postgres the pool is
`pool_size=20 + max_overflow=20` per worker
(`backend/app/settings/database.py:256-258`), so a handful of users sitting
on /agents or report pages pins the pool for minutes at a time —
`agents-page-contention.md` already showed the whole API collapses once
in-flight requests exceed the pool. That is the "entire system is extremely
slow" mechanism: every endpoint queues behind a pool starved by sweeping
detail requests.

Side effect (correctness, not just perf): each failed test flips the
connection to `is_active = False` for `system_only` connections
(`connection_service.py:699-706`). A transient network blip while a user has
/agents open silently deactivates connections org-wide — observed below
(`50 × is_active=0` after one sweep).

### B. The agent tree loads the FULL 150k-row catalog unpaginated (~200 MB)

Clicking the agent also calls `loadAgentMeta` →
`GET /data_sources/{id}/full_schema` **without pagination params**
(`frontend/components/KnowledgeExplorer.vue:2191`). The route treats
missing `page`/`page_size` as the legacy path
(`backend/app/routes/data_source.py:199,233-236`) →
`get_data_source_schema(include_inactive=True)`
(`backend/app/services/data_source_service.py:2528`) →
`DataSource.get_schemas` (`backend/app/models/data_source.py:246-375`), which
`selectinload`s **every** `DataSourceTable` row with its
`connection_table → connection` chain (`data_source.py:258-264`) and builds a
`Table` object per row. For this shape that is 150,000 rows → measured **36.1s
and a 199.4 MB JSON response** (ORM hydration dominates: 21.1s / 166 SQL at
the 75k-row scale, serialization adds the rest). The browser then parses
~200 MB and maps it (`items.map` in `loadAgentMeta`) — the tab freezes too.

The same data through the paginated branch of the same route takes **0.58s /
90 KB** (that's what `TablesSelector.vue` uses). Only `KnowledgeExplorer`
still uses the unpaginated call.

Because hydration/serialization run on the event loop between awaits, one
`full_schema` request stalls **every** request on the process: a 2-3ms
`/api/settings` probe measured **4.0s / 8.7s / 3.4s** while it was in flight.
Second "entire system" mechanism.

### C. `/schema` (chat @-mentions, agent flyout) hydrates 150k rows to return 100

`GET /data_sources/{id}/schema` returns only *active* tables (~100, 132 KB) —
but `DataSource.get_schemas` does the `is_active` filtering **in Python after
loading every row** (`backend/app/models/data_source.py:282-284`; only
`include_inactive` differs from path B). Measured: **22.9s to return 132 KB.**
This endpoint fires from the chat prompt's mention autocomplete
(`frontend/components/prompt/MentionInput.vue:1242`) and the agent flyout
(`frontend/components/AgentFlyout.vue:528`) — i.e. from ordinary chat usage,
not just /agents. Contrast: `SchemaContextBuilder` (the completion path)
pushes the same filter into SQL
(`backend/app/ai/context/builders/schema_context_builder.py:85-87`) and is
fine; `DataSource.prompt_schema` (`data_source_service.py:4016-4018`) still
goes through the Python-filtered `get_schemas`.

## Loop A — deterministic reproduction (no external services)

Fresh sandbox, Python 3.12, SQLite:

```bash
cd backend
pip install uv && uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_SMTP_PASSWORD=dummy ANTHROPIC_API_KEY=dummy
mkdir -p db && rm -f db/app.db db/app.db-wal db/app.db-shm
uv run alembic upgrade head
uv run python main.py &                      # backend :8000
uv run python scripts/slow_tcp_stub.py 1.0 & # "slow warehouse" on 127.0.0.1:55445
```

Register the sandbox admin and capture credentials:

```bash
B=http://localhost:8000
curl -s -X POST $B/api/auth/register -H "Content-Type: application/json" \
  -d '{"email":"sandbox@bow.dev","password":"Sandbox123!","name":"Sandbox Admin"}'
TOK=$(curl -s -X POST $B/api/auth/jwt/login -d "username=sandbox@bow.dev&password=Sandbox123!" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
ORG=$(curl -s $B/api/organizations -H "Authorization: Bearer $TOK" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['id'])")
```

Seed the customer shape — 50 connections × 3,000 tables, 2 active per
connection (100 org-wide), ALL on one "hub" agent plus one empty agent
(prints the hub agent id):

```bash
uv run python scripts/seed_one_agent_many_conns.py 50 3000 2
HUB=<hub_agent id printed by the seed>
H1="Authorization: Bearer $TOK"; H2="X-Organization-Id: $ORG"
```

**B + C — catalog hydration** (fires on agent click / chat mention):

```bash
curl -s -o /dev/null -w "full_schema (KnowledgeExplorer): %{time_total}s %{size_download}B\n" \
  -H "$H1" -H "$H2" "$B/api/data_sources/$HUB/full_schema"
curl -s -o /dev/null -w "full_schema (paginated):         %{time_total}s %{size_download}B\n" \
  -H "$H1" -H "$H2" "$B/api/data_sources/$HUB/full_schema?page=1&page_size=100"
curl -s -o /dev/null -w "schema (mentions/flyout):        %{time_total}s %{size_download}B\n" \
  -H "$H1" -H "$H2" "$B/api/data_sources/$HUB/schema"
# while full_schema is in flight, probe any cheap endpoint from another shell:
curl -s -o /dev/null -w "%{time_total}s\n" -H "$H1" -H "$H2" $B/api/settings
```

**A — the stale-connection sweep** (backdate the 5-minute TTL, then open the
agent twice concurrently):

```bash
python3 - <<'EOF'
import sqlite3
db = sqlite3.connect("db/app.db")
db.execute("UPDATE connections SET last_connection_checked_at=datetime('now','-1 hour')")
db.commit()
EOF
curl -s -o /dev/null -w "detail 1st: %{time_total}s\n" -H "$H1" -H "$H2" "$B/api/data_sources/$HUB" &
sleep 3
curl -s -o /dev/null -w "detail 2nd: %{time_total}s\n" -H "$H1" -H "$H2" "$B/api/data_sources/$HUB"
```

**No-connect-timeout evidence** (why 1s/test is the *optimistic* case):

```bash
timeout 20 uv run python -c "
import sqlalchemy; sqlalchemy.create_engine('postgresql://u:p@10.255.255.1:5432/x').connect()" \
  || echo "still connecting after 20s — OS TCP timeout (~2min) is the only bound"
```

### Observed (local SQLite, zero network latency, 1s/test stub)

| call | wall | payload | notes |
|---|---|---|---|
| `GET /data_sources` (list) | 0.05s | 49 KB | earlier fixes hold |
| `GET /connections` (list) | 0.36s | 35 KB | earlier fixes hold |
| `GET /data_sources/{empty agent}` | 0.01s | 1 KB | contrast |
| `GET /data_sources/{hub}` within TTL | 0.06s | **1.5 MB** | indexing events × 50 conns |
| `GET /data_sources/{hub}` after TTL (sweep) | **60.4s** | 1.5 MB | 50 × ~1.1s sequential tests |
| `GET /data_sources/{hub}` 2nd, concurrent | **59.6s** | 1.5 MB | no dedup — its own sweep |
| `GET .../full_schema` (as KnowledgeExplorer calls it) | **36.1s** | **199.4 MB** | 150k rows hydrated + serialized |
| `GET .../full_schema?page=1&page_size=100` | 0.58s | 90 KB | same data, paginated branch |
| `GET .../schema` (mentions/flyout) | **22.9s** | 132 KB | 150k rows loaded → 100 returned |
| `/api/settings` idle | 0.002s | — | baseline |
| `/api/settings` during `full_schema` | **4.0s / 8.7s / 3.4s** | — | event loop blocked |

After one sweep against the stub:
`SELECT last_connection_status, is_active FROM connections` →
`('not_connected', 0) × 50` — **all 50 connections silently deactivated** by
a transient failure (`connection_service.py:699-706`).

The blackhole check confirms `psycopg2` is still waiting at 20s: with no
`connect_timeout`, each unreachable connection costs ~2 minutes of OS TCP
timeout, so the production sweep is bounded at ~50 × 2min ≈ **100 minutes**,
re-armed every 5 minutes — "takes forever" is literal.

### Why the *whole* system degrades

1. **Event loop stalls**: hydrating/serializing 150k ORM rows (B, C) blocks
   the process between awaits — measured 8.7s stalls on a 2ms endpoint. Every
   user on that worker feels it, on every page.
2. **Pool pinning**: a sweeping detail request (A) holds its pooled DB
   connection for its full 60s–minutes wall time
   (`dependencies.py:48-61`; no `release_request_db` on this path). With
   `pool_size+overflow = 40` per worker (`settings/database.py:256-258`), a
   few open /agents or report tabs starve the pool and every other endpoint
   times out — the collapse mechanism already demonstrated in
   `agents-page-contention.md`.
3. **Triggered from everywhere**: the sweep and the catalog loads fire not
   only from `/agents` (`KnowledgeExplorer.vue:1156,2191`) but from report
   pages (`ReportAgentPanel.vue:936`) and the chat prompt's mention
   autocomplete (`MentionInput.vue:1242`), so ordinary chat usage keeps
   re-detonating them.

## The fix (A and B applied)

- **A — reads never dial.** The stale-retest block is deleted from
  `DataSourceService.get_data_source`; the endpoint serves the cached
  `last_connection_status` unconditionally. Freshness moved to a background
  sweeper, `backend/app/services/connection_status_sweep.py`
  (`sweep_stale_connection_status`), registered in `main.py` every 5 minutes.
  It reuses the `scheduled_reindex` machinery (`claim_scheduled_run`, one
  worker per fire) but with its **own TTL** (`BOW_CONN_STATUS_TTL`, default
  300s) decoupled from per-connection reindex schedules — reindex cadence is
  daily/weekly, status wants minutes, and reactivation of an auto-deactivated
  connection rides on the next successful test. Tests run concurrently
  (semaphore, `BOW_CONN_STATUS_SWEEP_CONCURRENCY`, default 8), each on its own
  short-lived session, bounded per tick (`BOW_CONN_STATUS_SWEEP_BATCH`,
  default 200), `system_only` connections only (per-user status is resolved
  per user at read time). Not enterprise-gated — with the read-path retest
  gone this is what keeps badges honest on every install.
- **B — the tree fetches one selected-only page.**
  `KnowledgeExplorer.loadAgentMeta` now calls
  `full_schema?page=1&page_size=500&selected_state=selected` (the paginated
  branch pushes `is_active` into SQL) and reads `tables`/`total` from
  `PaginatedTablesResponse`. A new `agentTableTotals` map backs the tree and
  overview counts so they don't depend on the capped page length. Note the
  overview "N tables" badge now counts **active** tables (previously the full
  catalog including unselected rows).

Deliberately NOT included (agreed scope): client connect timeouts (a dead
host still costs ~2 min per dial — now only inside the background sweep) and
the single-failure auto-deactivation guard (a blip still flips
`is_active=False`, healing on the next successful sweep within one TTL).

### Re-run — observed after the fix (same seed, same stub)

| call | before | after |
|---|---|---|
| `GET /data_sources/{hub}`, all 50 connections stale | **60.4s** | **0.09s** |
| `GET .../full_schema` as KnowledgeExplorer calls it | **36.1s / 199.4 MB** | **0.52s / 89 KB** (100 active tables, `total_tables=150000` intact) |
| `/api/settings` while the above run | up to **8.7s** | **~0.003s** (flat) |
| status refresh for 50 stale connections | in-request, sequential | background sweep, all 50 refreshed in one pass (~23s vs 60s in-request), zero requests impacted |

Trigger the sweep manually to verify (the scheduled job does the same thing
every 5 minutes):

```bash
uv run python - <<'EOF'
import asyncio
import app.models, pkgutil, importlib
for _, m, _ in pkgutil.iter_modules(app.models.__path__):
    if m != "application": importlib.import_module(f"app.models.{m}")
from app.services.connection_status_sweep import sweep_stale_connection_status
asyncio.run(sweep_stale_connection_status())
EOF
# then: SELECT count(*) FROM connections
#       WHERE last_connection_checked_at <= datetime('now','-300 seconds')  → 0
```

## Remaining candidate fixes (not implemented)

- **C:** push the `is_active` filter into SQL in `DataSource.get_schemas`
  (as `SchemaContextBuilder.build` already does) so `/schema` (mention
  autocomplete, agent flyout — still **22.9s** at this shape) and
  `prompt_schema` stop hydrating the inactive catalog.
- Client connect timeouts (e.g. `connect_args={"connect_timeout": 5}`) so the
  background sweep isn't bounded by the ~2-minute OS TCP timeout per dead
  host.
- Auto-deactivation guard: require consecutive failures before flipping
  `is_active=False` on `system_only` connections.
- Detail payload: drop `indexing.events` from `GET /data_sources/{id}` unless
  an indexing run is live (1.5 MB per fetch with 50 connections).

## What this proves / regression notes

- The earlier list-endpoint fixes (`agents-page-connections-perf.md`) hold at
  this scale; the remaining slowness is on the detail/catalog paths above.
- All three causes scale with *per-agent* connection/table volume, which is
  why concentrating 50 connections on one agent (vs round-robin across 3)
  regressed the page after the earlier fixes.
- The seed initially wrote `type="postgres"` (registry name is
  `postgresql`) which made every test fail in ~3ms with "Unknown data source
  type" — masking cause A entirely. `scripts/seed_one_agent_many_conns.py`
  seeds the registry name and real dialable configs; if you fork the loop,
  keep that, or the sweep looks cheap.

## Repro artifacts

- `backend/scripts/seed_one_agent_many_conns.py` — the customer shape:
  N connections × N tables (N active) all on one agent, + one empty agent.
- `backend/scripts/slow_tcp_stub.py` — deterministic "slow warehouse":
  accepts TCP, stalls, closes; each connection test costs ~DELAY seconds.
