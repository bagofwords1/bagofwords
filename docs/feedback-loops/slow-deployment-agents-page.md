# Feedback Loop — "extremely slow deployment… /agents is very slow, sometimes the root page is slow"

Investigation of a production deployment (`bow.fattal.co.il`) reported as broadly
slow. The tenant has **~50 databases (Connections) attached to 2 agents
(DataSources)**. Goal: identify the slow requests and the root cause.

> **Scope note on the live scan.** The Playwright leg (Loop B) could **not** be
> run from the investigation sandbox: the deployment's HTTPS listener resets the
> connection from our egress IP (`:443` → TCP RST after ~11s on every attempt),
> which is consistent with an **IP allowlist / firewall** in front of the app —
> expected for an enterprise tenant. What *did* answer from the edge is itself a
> signal (see Loop B). The root-cause analysis below is from the code paths the
> two slow pages actually hit; the fix targets those. Re-run Loop B from inside
> the corporate network / an allowlisted IP to capture the live waterfall.

## Root cause (validated from code)

The slowness is **multi-factor** — an application-level O(N-connections) query
fan-out *amplified* by an under-provisioned / co-located runtime. Ranked:

### 1. Three overlapping list endpoints re-walk all ~50 connections on every `/agents` load

`/agents` → `KnowledgeExplorer.vue` `onMounted` fires 7 parallel requests
(`frontend/components/KnowledgeExplorer.vue:2641`), and the default layout
additionally fires `initAgent()` → `GET /data_sources`
(`frontend/layouts/default.vue:539` → `frontend/composables/useAgent.ts:215`).
Three of those independently walk the full connection set:

- `GET /data_sources/active?include_unconnected=true` → `get_active_data_sources` (`backend/app/services/data_source_service.py:1250`)
- `GET /data_sources` (from the layout) → `get_data_sources` (`backend/app/services/data_source_service.py:1138`)
- `GET /connections` → `list_connections` (`backend/app/routes/connection.py:115`)

The first two both call `_build_connections_list`
(`backend/app/services/data_source_service.py:201`), which **loops over every
connection** (`:274`).

### 2. Per-connection work inside that loop is only cheap for `system_only` connections

For each connection the loop calls `build_user_status_for_connection`
(`backend/app/services/user_data_source_credentials_service.py:194`):

- **`auth_policy == "system_only"` (the default): ~free** — returns the cached
  status with zero queries (`user_data_source_credentials_service.py:223-231`),
  and the per-user overlay-count block is skipped.
- **`auth_policy == "user_required"`: expensive per connection** — 2–3 serial
  awaited DB round-trips each: `get_primary_active_row` (`:256`), a
  `UserConnectionCredentials` lookup (`:261`), plus a **per-user overlay
  `COUNT(DISTINCT …)`** back in the loop (`data_source_service.py:343-367`).

With `user_required` connections this is on the order of **~100–150 serial
awaited DB queries per `/agents` load** (≈50 connections × 2–3, times the two
`_build_connections_list` callers, plus `/connections` doing its own per-user
overlay count at `connection.py:282-290`). All three run concurrently and
contend for the same pooled DB connections.

> **The single most important thing to confirm: the `auth_policy` of the 50
> connections.** If they are `system_only`, this path is already cheap and the
> dominant factors are #3/#4 below. If they are `user_required`, this fan-out is
> the primary cause. (Earlier N+1s for indexing rows and table counts were
> already batched via `_bulk_connection_aux`, `data_source_service.py:102`; the
> user-status/overlay work is the part that was *not* batched.)

### 3. Very low worker count — everything shares one (or few) event loops

`start.sh` sets `WORKERS = min(4, CPUs/2)`, floor 1
(`start.sh` "Calculate workers"). On a **2-vCPU** box that is **1 uvicorn
worker**, and that single process serves the **SPA static bundle, the API, and
the in-process scheduler all at once** (`SERVE_FRONTEND=1`; uvicorn is the only
foreground process). So a burst of the heavy list endpoints above blocks
*everything*, including delivery of the JS bundle — which is exactly why the app
"feels slow" globally and why even the light root page stalls when a neighbour
is loading `/agents`. Override: `UVICORN_WORKERS`.

### 4. The status sweeper is co-located and can periodically stall the leader worker

The scheduler is an **`AsyncIOScheduler` running inside the web worker's event
loop** (`main.py:522`, `app/core/scheduler.py:22`). Every 5 minutes
`sweep_stale_connection_status` (`main.py:487`,
`app/services/connection_status_sweep.py:64`) dials up to 200 stale
`system_only` connections, 8 concurrent, each dial occupying a thread from the
process-wide `asyncio.to_thread` pool for its full timeout
(`connection_status_sweep.py:51-54`). That pool defaults to `min(32, CPUs+4)` —
only **~6 threads on a 2-vCPU box** — and **query execution shares it**. If any
of the 50 databases are unreachable/slow (likely with 50 warehouses), a sweep
tick holds those threads until their socket timeout, starving request handling
on the scheduler-leader worker → the intermittent "**sometimes the root page is
slow**" spikes.

## Loop A — deterministic reproduction (no live credentials)

Static, in-sandbox: the per-connection fan-out is visible directly.

```bash
cd backend
# The loop that scales with connection count:
sed -n '274,368p' app/services/data_source_service.py
# The 3 endpoints that each drive it on a single /agents load:
sed -n '1138,1260p' app/services/data_source_service.py   # get_data_sources / get_active_data_sources
sed -n '250,290p'  app/routes/connection.py               # list_connections per-user overlay COUNT
```

Observed: `data_source_service.py:274` iterates `data_source.connections`; for
`user_required` connections each iteration awaits `get_primary_active_row`
(`user_data_source_credentials_service.py:256`), a `UserConnectionCredentials`
select (`:261`), and the overlay `COUNT(DISTINCT)` (`data_source_service.py:343`)
— i.e. query count grows linearly with connection count, per endpoint, ×3
endpoints per `/agents` load.

A stronger regression harness (to add): seed one org, one `user_required`
DataSource with N connections + per-user overlay rows, call
`get_data_sources(current_user=member)` under a SQLAlchemy `before_cursor_execute`
counter, and assert the query count does **not** grow with N (it currently does).

## Loop B — live confirmation (blocked by tenant firewall; what the edge showed)

From the sandbox egress IP, repeated probes:

```
GET https://bow.fattal.co.il/           :443 → TCP reset after ~11.4s (every attempt)
GET http://bow.fattal.co.il/            :80  → HTTP 503 after ~15.1s
GET http://bow.fattal.co.il/api/health  :80  → HTTP 503 after ~15.1s
```

- `:443` reset ⇒ our IP is not allowlisted (firewall) — run Loop B from inside
  the network.
- `:80` returning **503 only after ~15s** is meaningful: Caddy `reverse_proxy
  app:3000` (`Caddyfile`) waited on the upstream and gave up — i.e. the backend
  was **not answering health in time**, the fingerprint of a saturated /
  single-worker app. Capture this properly with the committed harness
  `tools/agent/measure_page_timings.mjs` (drives the sign-in form, records every
  request's timing for `/` and `/agents`, prints the slowest requests) once run
  from an allowlisted host:
  `BOW_EMAIL=… BOW_PW=… node tools/agent/measure_page_timings.mjs`.

Expected live waterfall to confirm from inside the network: `/data_sources`,
`/data_sources/active`, and `/connections` as the top-3 slowest XHRs on
`/agents`, each in the hundreds-of-ms-to-seconds range and growing with
connection count; `/data_sources` (via `initAgent`) as the slow one on `/`.

## The fix (recommended, in priority order)

**Immediate, infra (no code) — do these first, they are the cheapest wins:**
1. Set `UVICORN_WORKERS` to ≥4 (and give the container ≥4 vCPU). One worker
   serving SPA+API+scheduler is the biggest amplifier.
2. Run the scheduler **out of the request path** — a dedicated non-serving
   replica/process holds the `scheduler_leader` lease
   (`try_acquire_scheduler_leader`, `main.py:401`) so warehouse dialing never
   competes with request threads. As a stopgap, lower
   `BOW_CONN_STATUS_SWEEP_CONCURRENCY` / raise `BOW_CONN_STATUS_TTL` and tighten
   the per-dial connect timeout so a dead database can't hold a thread for 15s.

**Application (code) — removes the O(N) itself:**
3. Bulk-load the per-connection user status once across all connections
   (mirror the existing `_bulk_connection_aux` batching, `data_source_service.py:102`):
   one query each for `UserDataSourceCredentials`, `UserConnectionCredentials`,
   and the per-user overlay counts keyed by `connection_id`, then have
   `build_user_status_for_connection` / the overlay block read from those maps
   instead of querying per connection (`data_source_service.py:274-368`,
   `connection.py:258-290`).
4. De-duplicate the redundant list calls on `/agents`: the page loads
   `/data_sources/active` while the layout separately loads `/data_sources` — two
   full walks of the same connections per navigation. Serve the layout's agent
   list from the page's data, or add a lightweight shared/cached endpoint.

Confirm the connections' `auth_policy` first: `system_only` ⇒ prioritise #1/#2;
`user_required` ⇒ #3 is the headline fix.

## What this proves / notes

- The slow requests are the three connection-list endpoints hit on `/agents`
  (and `/data_sources` on `/`); the *reason* is per-connection user-status/overlay
  queries that scale with connection count, magnified by a 1-worker,
  scheduler-co-located runtime.
- Live Playwright confirmation is pending network access from an allowlisted IP;
  the harness and the exact endpoints to watch are specified above so it can be
  run in one shot from inside the tenant network.
