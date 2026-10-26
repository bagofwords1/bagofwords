# MSSQL indexing fails with "Indexing stopped responding"

Status: diagnosis, not fixed. Reproduced locally (see [Reproduction](#reproduction)).
The evidence points to the worker process dying, most likely from memory, not
to a blocked heartbeat. Not yet confirmed from the customer's data.

## Symptom

Indexing an MSSQL connection fails with:

> Indexing stopped responding (no progress for 13 minute(s)) — the server most
> likely restarted during the run. Reindex to start it again.

Deployment: Kubernetes, 2 replicas, uvicorn with multiple workers per pod
(`start.sh:199`). We have no access to the customer's cluster.

## How indexing runs

```
browser ── POST /connections/{id}/reindex ──► any worker (main HTTP loop)
                                               start(): INSERT row (pending),
                                               run_coroutine_threadsafe(_run) ─┐
                                                                              ▼
                              same worker, "connection-indexing-loop" thread (bg loop)
                                _run: create_task(_heartbeat)   every 30s UPDATE updated_at
                                      gate (max 4 per worker)
                                      _run_inner → refresh_schema
                                        ├ aget_schemas → to_thread(get_schemas)   pool thread
                                        │    MSSQL catalog query, fetchall, build Tables
                                        ├ normalize, upsert loop                  bg loop, no await
                                        └ one commit                              bg loop
browser ── GET /connections/{id}/indexing every 2s ──► any worker/pod
                                               get_latest → _reap_if_stale
```

- An indexing run lives only in the worker process that received the POST
  (`ConnectionIndexingService.start`, `connection_indexing_service.py:440`).
- `_heartbeat` (`:501`) is an asyncio task on that worker's background loop. It
  writes `updated_at` to the shared DB every 30s.
- `_reap_if_stale` (`:344`) runs in whatever process reads the row (UI polls,
  `start()`, cancel). It marks a pending/running row failed with this message
  once `updated_at` is older than `_STALE_AFTER_SECONDS` (600s, `:186`).

## What the message proves

- **Nothing wrote to the row for about 13 minutes**, measured at the read that
  reaped it. `updated_at` has `onupdate=datetime.utcnow` (`models/base.py:23`),
  so the heartbeat, progress flushes, event appends and status changes all set
  it. The whole runner was silent, not only the heartbeat. "No progress" is
  misleading: `last_activity_at` is not checked.
- **13 is the time since the last write, not how long the outage lasted.** The
  row is only checked when read. With the UI polling every 2s the error would
  read about 10, so nobody read it between minute 10 and minute 13.
- **It does not prove the process died.** A blocked background loop or a long
  app-DB outage looks the same (heartbeat write failures are logged only at
  debug level, `indexing.heartbeat_failed`).
- The age is computed with `utcnow()` in the reading pod against a timestamp
  written by the writing pod. Clock skew between nodes would distort it.

## Ruled out: a slow MSSQL query alone

- MSSQL discovery runs one large catalog query with no progress until it
  returns (`backend/app/data_sources/clients/mssql_client.py:235-296`,
  `.fetchall()` at `:274`). It runs off the loop, in a worker thread
  (`DataSourceClient.aget_schemas` → `asyncio.to_thread`, `clients/base.py:219`),
  so the heartbeat keeps running.
- Verified: a discovery blocked in its thread past the stale window is not
  reaped (`backend/tests/unit/test_connection_indexing_liveness.py`, and the
  throwaway repro below).
- A quiet discovery would be stopped by the idle timeout (900s, `:203`) with a
  different message ("Schema discovery made no progress…").

## Ruled out in practice: the save step blocking the heartbeat

After discovery, `refresh_schema` normalizes the catalog, builds ORM objects and
commits once (`connection_service.py:1875-2016`). This runs **on** the
background loop with no `await` until the commit, so it does block the
heartbeat while it runs. Measured with real code on catalogs up to 2M columns
(see [Reproduction](#reproduction)), the longest block is about 4s per million
columns: 8.2s at 2M. Reaching 600s would take on the order of 100M+ columns.

## When the heartbeat stops

In each case below the run is lost, and about 10 minutes later the row shows
this error. For this MSSQL failure, **out of memory** is the prime suspect (see
the measurements), then a **deploy or rolling restart**.

### The process is killed

1. **Out of memory.** Peak memory of the indexing worker grows by about
   2.3 GB per million catalog columns (measured). Several full copies of the
   catalog are alive at once:
   - the previous catalog loaded from the DB (`existing_tables`);
   - `fetchall()` rows, with the `NVARCHAR(MAX)` table description repeated on
     every column row of that table;
   - `Table` / `TableColumn` objects, built while the rows are still referenced;
   - the normalized `incoming` dict, built while `fresh_tables` is still alive;
   - ORM objects and unit-of-work state for the whole catalog, until the one
     commit.

   If the enriched query fails partway, `_get_tables_basic()` runs a second
   full fetch. The chart requests 900Mi and sets no limit by default
   (`k8s/chart/values.yaml:303-306`), with up to 4 uvicorn workers per pod:
   - with a pod memory limit, the kernel kills the largest process, which is
     the one indexing (`OOMKilled`, container restart, all workers die);
   - with no limit, the node's OOM killer can pick the process, or the kubelet
     evicts the pod under node memory pressure.
2. **Liveness probe failure.** `/health`, `timeoutSeconds: 1`, `periodSeconds:
   20`, `failureThreshold: 3` (`k8s/chart/values.yaml:409-417`). About 60s of
   slow answers restarts the container. CPU throttling or GIL contention from a
   large save step can cause this.
3. **uvicorn worker health check.** uvicorn `>=0.31.1,<0.32` pings each worker
   and kills one that doesn't answer within about 5s. Log line:
   `Child process [pid] died`.
4. **Native crash.** A segfault in pyodbc, msodbcsql or the Kerberos/GSSAPI
   libraries takes the whole worker down. Unlikely, but it looks the same from
   the outside.

### The pod is stopped on purpose

5. **Deploy or rolling restart.** `helm upgrade`, a new image, a config or
   secret change that rolls the pods, `kubectl rollout restart`.
6. **Scale-down.** With autoscaling on, a pod removed during scale-down takes
   its runs with it.
7. **Node maintenance or loss.** Node drain, cluster upgrade, a spot or
   preemptible node reclaimed, a node crash.
8. **Manual restart** or deleting the pod.

### The process is alive but the heartbeat stops

9. **Background loop blocked.** All indexing runs in one worker share one
   background loop, and the heartbeat runs on it. Synchronous work on that loop
   (the save step above, or a connector that makes a blocking call without
   `to_thread`) stops every heartbeat in that worker. For MSSQL the save step
   is the only such code, and it is far too short to reach 600s.
10. **Heartbeat can't reach the app database for 10+ minutes.** A Postgres
    outage or failover, network problems, expired IAM auth tokens.

## How to confirm without cluster access

A blocked runner keeps running after it is reaped, and leaves traces after the
reap. A dead worker leaves nothing. Ask the customer's admin for:

1. **The run:** `GET /api/connections/{id}/indexing` as a connection manager
   (the event log is withheld from other users). It returns only the latest
   run, so get it before they retry, or use the SQL below.
2. **`last_synced_at`** from `GET /api/connections/{id}`. It is set in the same
   transaction as the catalog write, so it shows whether saving finished.
3. **Audit log** (`/settings/audit`): a `connection.schema_refreshed` entry
   after the run started means the catalog was committed.
4. **Catalog size:** `SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS` for the
   indexed schemas, to compare against the memory measurements.
5. **Is the connection's Schema field empty?** If so, every schema in the
   database is indexed (see [Comparison with Oracle](#comparison-with-oracle)),
   and the count in 4 should cover the whole database.
6. **Pod memory limit and `UVICORN_WORKERS`**, from their Helm values.

| Evidence | Loop blocked (process alive) | Worker died |
|---|---|---|
| Run `status` now | `completed` (or `cancelled`, see below) | still `failed` |
| `events` after `finished_at` | yes, e.g. "Completed: N tables in Xs" | none |
| `connection.last_synced_at` | after the run's `started_at` | before `started_at` |
| `connection.schema_refreshed` audit entry | yes | no |
| Catalog | the new one | unchanged |

Do not use `phase` to decide how far the run got: when the loop is blocked or
the process dies, the last progress flushes may never be written (see below).

If they can run SQL against the app database:

```sql
SELECT id, status, error, phase, started_at, finished_at, updated_at, last_activity_at,
       stats_json->>'elapsed_s' AS elapsed_s, events_json
FROM connection_indexing
WHERE connection_id = '<id>'
ORDER BY created_at DESC LIMIT 5;

SELECT last_synced_at FROM connections WHERE id = '<id>';
```

If we do get cluster access: `kubectl get pods` (RESTARTS, pod age vs the run's
`started_at`), `kubectl describe pod` (`OOMKilled`, `Liveness probe failed`),
and backend logs (`Child process [...] died`, `indexing.reaped_stale`).

## Reproduction

Throwaway scripts, not committed. They run the real `ConnectionIndexingService`
runner, the real `ConnectionService.refresh_schema` and the real
`MSSQLClient._get_tables_enriched`. Only the SQL Server connection is faked:
`MSSQLClient.connect()` yields a fake connection whose catalog query returns
rows shaped like the production query, with the table description repeated on
every column row. App DB: SQLite (WAL) file. Timers shrunk to a 0.2s heartbeat
and a 2s stale window, so the message reads "0 minute(s)".

### The two cases

The UI poller runs in a different process from the worker running the job, as
with several workers or pods. 300 tables × 20 columns.

| After the error | Loop blocked 6s in saving | Worker SIGKILLed in saving | Worker SIGKILLed in discovery |
|---|---|---|---|
| UI showed | "stopped responding" | "stopped responding" | "stopped responding" |
| `status` | `completed` | `failed` | `failed` |
| `error` | "stopped responding" (kept) | "stopped responding" | "stopped responding" |
| `last_synced_at` after `started_at` | yes | no (null) | no (null) |
| Tables in the catalog | 300 | 0 | 0 |
| Events after `finished_at` | "Completed: 300 tables in 6.4s" | none | none |
| `phase` | `syncing_agents` | `connecting` | `reading_columns` |

### Loop block and memory by catalog size

No artificial delays. "Longest loop block" is the longest gap in which no
coroutine on the background loop, the heartbeat included, could run (measured
with a probe task on that loop). Table descriptions 500 characters. 4-CPU
container.

| Columns | Tables × columns | Longest loop block | Discovery (thread) | Total run | Peak memory of the worker |
|---|---|---|---|---|---|
| 30k | 1,000 × 30 | 0.1s | 0.4s | 1.1s | 0.57 GB |
| 200k | 5,000 × 40 | 1.3s | 2.8s | 6.6s | 0.95 GB |
| 1M | 20,000 × 50 | 3.9s | 14.9s | 30s | 2.6 GB |
| 2M | 40,000 × 50 | 8.2s | 29.1s | 63s | 5.0 GB |

Baseline memory of the process before the run: 0.49 GB.

### Limits

- SQLite instead of Postgres. Postgres adds network round-trips that yield the
  loop, so blocks there should be shorter if anything.
- Fake rows are Python tuples. pyodbc rows are heavier, so real memory is
  likely higher.
- Discovery time excludes SQL Server's own query time, which on a large
  database can be many minutes.
- A native crash (pyodbc, msodbcsql, Kerberos) is not covered; that needs a
  real SQL Server.

## Defects found while reproducing

1. **A recovered run ends `completed` but keeps the reaper's error.** At
   finalize (`connection_indexing_service.py:1033`), `db.get` returns the
   runner's cached row (status `running`, error `None`) without re-reading it.
   Setting `status = completed` is written, but `error = None` is no change
   from the cached value, so it is not written. The row ends `completed` with
   "stopped responding" in `error`. Going by the code, not checked in a
   browser: the compact view of `ConnectionIndexingProgress.vue` (`:112-120`)
   lists `error` as a red problem on any non-failed run.
2. **If the reaper runs in the same worker as the run, the run ends
   `cancelled` after saving the catalog.** `_reap_if_stale` also sets the run's
   in-process cancel event. The runner still commits the new catalog
   (`last_synced_at` updated), then hits the cancel check before
   `_sync_linked_data_sources` and stops. Linked data sources (agents) keep the
   old table list.
3. **Progress can be lost.** Progress callbacks fire from the discovery thread
   but are written by a flush task on the background loop, at most every
   0.25s. If the loop blocks in the save step right after discovery, the last
   discovery progress and the `saving` phase are never written. In the "killed
   in saving" run, the row still showed `connecting`.

## Comparison with Oracle

The indexing pipeline is the same for both: runner, heartbeat, thread-pool
discovery, `refresh_schema`'s save step and `attach_foreign_keys`. The
differences are all in the client's `_get_tables_enriched`.

| | MSSQL (`mssql_client.py`) | Oracle (`oracledb_client.py`) |
|---|---|---|
| Catalog query | one query: `INFORMATION_SCHEMA.COLUMNS` LEFT JOIN `sys.columns` LEFT JOIN `sys.extended_properties` twice (`:251-272`) | three separate owner-filtered queries, merged in Python (`:213-237`) |
| Join keys | `OBJECT_ID(c.table_schema + '.' + c.table_name)`, computed per row, twice | none on the server; a dict lookup on `(owner, table, column)` |
| Table comment | `NVARCHAR(MAX)` repeated on every column row | fetched once per table (`all_tab_comments`) |
| Column comments | a column on every row, NULL or not | only non-null comments fetched |
| Main result row | 6 fields, 2 of them `NVARCHAR(MAX)` | 4 short fields |
| Scope with the Schema field empty | the whole database: `table_catalog = :database`, every schema the login can see | the login user's own schema: `owner = :user` (`_owner_filter`, `:166-178`) |
| `fetchall()` | yes | yes, on all three queries |
| Connection test | no `count_tables()`, so the test runs the full `get_schemas()` crawl | `count_tables()`: `COUNT(*)` on `all_tables` + `all_views` (`:180-196`) |
| Basic fallback on failure | `_get_tables_basic` raises, so the run fails | `_get_tables_basic` swallows the error and returns `[]` (`:300-305`), so the run completes with 0 tables and nothing is pruned |

Oracle was rewritten in `62de15961` (2026-10-07). On Oracle 10g/11g, the
three-way LEFT JOIN over the `ALL_*` views ran for more than 2 minutes on a
warehouse with 137k columns, while each view queried on its own answered in
1–2s. The same commit added `count_tables()` and the connection-test hook
`_acount_tables_for_validation` (`connection_service.py:209`). Tests:
`tests/unit/test_oracledb_schema_discovery.py`,
`tests/unit/test_schema_validation_table_count_fast_path.py`. This is the
in-repo precedent for P1.2 and P1.6 below.

What it means for this bug:

- MSSQL carries more data per column, mostly the table description repeated
  on every row.
- With the Schema field empty, MSSQL indexes far more than Oracle would on the
  same database, which matters directly for memory.
- The per-row `OBJECT_ID()` join is the same kind of server-side slowdown
  Oracle's split fixed. A longer run is more likely to be cut off by a
  restart.
- Testing a large MSSQL connection runs a full catalog fetch on the HTTP
  request path. On create this can mean two full crawls in memory at once, in
  different workers (not measured).
- Oracle still has the same `fetchall()` and all-at-once save step, so a large
  enough Oracle catalog would hit the same memory peak.

**Decision: keep the MSSQL default scope.** With the Schema field empty, MSSQL
keeps indexing every schema in the database. Narrowing it would drop tables
from existing connections at the next reindex, and the prune step would delete
them from the catalog and from agents. For a very large database, the
workaround is for the customer to fill in the Schema field with only the
schemas they need.

## Proposed fixes

Not implemented. In priority order. The root cause is not confirmed, so P0 is
the change that would confirm it on the customer's next run.

Suggested first step: P0 together with P1.1 and P1.4. They are small, low-risk
changes to two files, and they either confirm the memory theory on the next run
or prevent the failure. Then P1.2 and P1.3 (check against a real SQL Server),
then the P2 finalize fix.

### P0: make the next failure explain itself (diagnostics only)

We can't see the customer's cluster, but the customer can send us the run's
event log (`GET /api/connections/{id}/indexing`). Put the evidence there:

```python
# connection_indexing_service.py, _run_inner, around the discovery call
await _append_event("info", "connecting",
    f"Worker pid {os.getpid()}, rss {_rss_mb()} MB")
# refresh_schema, right after aget_schemas returns (via progress_callback / a hook)
#   "Discovered 41,200 tables / 1,930,000 columns, rss 3,150 MB"
# right before the commit
#   "Saving catalog, rss 4,900 MB"
```

```python
# _heartbeat: failures are invisible today (debug level)
except Exception:
    logger.warning("indexing.heartbeat_failed", exc_info=True)   # rate-limit if needed
```

If the next failure's last event is "rss 4,900 MB" with nothing after it, that
confirms an OOM kill without kubectl.

### P1: cut peak memory (the likely root cause)

1. **Don't keep the fetched catalog for the whole run.** `refresh_schema` saves
   the full fetched list on the service object:
   `self.last_refresh_fresh_tables = list(fresh_tables or [])`
   (`connection_service.py:1775`). Only the request path in
   `data_source_service.py:5971` reads it; the background runner never does,
   so the catalog stays in memory through saving, warming and agent sync. Add
   a flag so the background run skips it, and release the list once it is
   normalized:

   ```python
   async def refresh_schema(..., keep_fresh_tables: bool = True):
       ...
       if keep_fresh_tables:
           self.last_refresh_fresh_tables = list(fresh_tables or [])
       ...
       incoming = {...}            # normalize
       del fresh_tables            # Table objects no longer needed
   ```

   The runner (`connection_indexing_service.py:911`) passes
   `keep_fresh_tables=False`.

2. **Stop repeating the table description on every column row, and drop the
   per-row `OBJECT_ID()`.** Replace the single query in
   `mssql_client.py:251-272` with two queries joined on ids:

   ```sql
   -- columns: one row per column, no table description
   SELECT s.name, o.name, c.name, TYPE_NAME(c.system_type_id),
          CAST(ep.value AS NVARCHAR(MAX))
   FROM sys.objects o
   JOIN sys.schemas s ON s.schema_id = o.schema_id
   JOIN sys.columns c ON c.object_id = o.object_id
   LEFT JOIN sys.extended_properties ep
     ON ep.class = 1 AND ep.major_id = c.object_id AND ep.minor_id = c.column_id
    AND ep.name = 'MS_Description'
   WHERE o.type IN ('U', 'V') [AND s.name IN (...)]
   ORDER BY s.name, o.name, c.column_id;

   -- table descriptions: one row per table
   SELECT s.name, o.name, CAST(ep.value AS NVARCHAR(MAX))
   FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
   JOIN sys.extended_properties ep
     ON ep.class = 1 AND ep.major_id = o.object_id AND ep.minor_id = 0
    AND ep.name = 'MS_Description'
   WHERE o.type IN ('U', 'V') [AND s.name IN (...)];
   ```

   Must be checked to return the same tables and types as
   `INFORMATION_SCHEMA.COLUMNS`. Watch user-defined alias types and views,
   which `INFORMATION_SCHEMA` includes.

3. **Read rows from the cursor as they arrive instead of `fetchall()`:**

   ```python
   result = conn.execution_options(stream_results=True).execute(sql, params)
   for row in discovery_items(result, 'columns', ...):   # iterate, don't fetchall()
   ```

   The raw rows then never all exist at once next to the `Table` objects.

4. **Yield to the event loop during the save step.** Not a root-cause fix, but
   it keeps the heartbeat and progress flushes running, and fixes defect 3
   (lost progress):

   ```python
   for i, t in enumerate(fresh_tables):
       ...
       if i % 500 == 0:
           await asyncio.sleep(0)
   ```

   Same in the upsert loop (`connection_service.py:1925`).

5. **(Larger change) Commit the catalog in chunks.** Caps the ORM memory peak,
   but a crash would then leave a half-updated catalog instead of today's
   all-or-nothing commit, and pruning would have to run only after the last
   chunk. Only if 1–3 are not enough.

6. **Add `count_tables()` to `MSSQLClient`**, as Oracle has, so the connection
   test counts tables instead of running a full crawl on the HTTP request path.
   `_acount_tables_for_validation` (`connection_service.py:209`) already picks
   it up when the method exists:

   ```python
   def count_tables(self) -> int:
       where, params = "o.type IN ('U', 'V')", {}
       if self._schemas:
           keys = [f":s{i}" for i in range(len(self._schemas))]
           params = {f"s{i}": s for i, s in enumerate(self._schemas)}
           where += f" AND s.name IN ({', '.join(keys)})"
       with self.connect() as conn:
           return int(conn.execute(text(
               "SELECT COUNT(*) FROM sys.objects o "
               "JOIN sys.schemas s ON s.schema_id = o.schema_id "
               f"WHERE {where} AND o.is_ms_shipped = 0"
           ), params).scalar() or 0)
   ```

   Must match the table count indexing reports (same caveat as P1.2 about
   what `INFORMATION_SCHEMA` includes).

The default scope with an empty Schema field stays as it is (see
[Comparison with Oracle](#comparison-with-oracle)). None of these fixes depend
on changing it.

### P2: the defects found while reproducing

1. **A completed run keeps the error, and a user cancel can be overwritten.**
   At finalize (`connection_indexing_service.py:1033`), re-read the row instead
   of using the cached copy:

   ```python
   fresh = await db.get(ConnectionIndexing, indexing_id, populate_existing=True)
   if fresh is None:
       return
   if fresh.status == ConnectionIndexingStatus.CANCELLED.value:
       return                       # a user cancel from another worker wins
   fresh.status = COMPLETED
   fresh.error = None               # now actually written: the value really changes
   ```

   A reaped run that finished correctly becomes `completed` with no error; a
   run a user cancelled stays cancelled.
2. **A reap in the same worker cancels a live run after it saves.**
   `_reap_if_stale` sets the local cancel event (`:372`), and the cancel check
   at `:1016` then skips the agent sync after the catalog is committed.
   Smallest fix: stop setting the event in the reaper and let finalize decide
   with the re-read above. Fuller fix: the runner re-reads its row status at
   checkpoints (P3.1), so a user cancel works from any worker.
3. **Message wording.** "No progress" points at the wrong field. For example:
   "The server stopped reporting on this run for 13 minutes (the worker
   probably restarted or ran out of memory)."

### P3: multi-replica issues (independent of the MSSQL failure)

1. **Cancel only works on the same pod.** `_cancel_events` (`:47`) is held in
   process memory. A cancel request that reaches another worker or replica:
   - marks the row `cancelled` in the DB;
   - does not stop the crawl, which keeps running in the first worker;
   - lets the row be overwritten as `completed` at finalize (`:1033`), the same
     stale read as defect 1 above;
   - lets a new run start meanwhile, so two crawls hit the same source.

   Fix: have the runner re-read the row status at its checkpoints (the
   heartbeat is a natural place to do this) and set the local cancel event when
   the row is terminal. At finalize, do not overwrite a terminal status.
2. **Duplicate runs.** `start()` (`:460`) checks for an active run and then
   inserts, with no DB constraint. Two near-simultaneous requests on different
   replicas can create two active runs. The scheduled reindex sweep makes this
   more likely: the scheduler leader is chosen with an `flock` on
   `/tmp/bow-scheduler.lock` (`core/scheduler.py:91`), which is per pod, so
   each replica runs its own sweep. Fix: a partial unique index on
   `(connection_id, user_id)` where status is pending/running (or a row lock),
   handling the conflict by returning the existing row.
3. **Concurrency limit is per worker.** `BOW_INDEXING_CONCURRENCY` (`:95`)
   applies to each uvicorn worker, not the whole deployment. Up to
   4 × workers × replicas crawls can run at once. Document this, or move the
   limit into the DB.
4. **No failover.** A run lost to a pod restart is only detected after 10
   minutes, and it is not resumed. A real fix needs a durable job queue, or
   resumable, checkpointed crawls.
