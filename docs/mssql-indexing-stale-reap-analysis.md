# MSSQL indexing fails with "Indexing stopped responding"

Status: diagnosis, not fixed. Root cause not yet confirmed from production logs.

## Symptom

Indexing an MSSQL connection fails with:

> Indexing stopped responding (no progress for 13 minute(s)) — the server most
> likely restarted during the run. Reindex to start it again.

Deployment: Kubernetes, 2 replicas, uvicorn with multiple workers per pod
(`start.sh:199`).

## What the error means

- An indexing run lives only in the worker process that started it.
  `ConnectionIndexingService._heartbeat`
  (`backend/app/services/connection_indexing_service.py:501`) writes
  `updated_at` to the shared DB every 30s.
- `_reap_if_stale` (`connection_indexing_service.py:344`) marks any
  pending/running row failed with this message once `updated_at` is older than
  `_STALE_AFTER_SECONDS` (600s, `:186`). It runs whenever the row is read, for
  example when the UI polls.
- So the heartbeat stopped for about 13 minutes. That happens only if the
  worker process died or its background event loop froze.

## Ruled out: a slow MSSQL query alone

- MSSQL discovery runs one large catalog query with no progress until it
  returns (`backend/app/data_sources/clients/mssql_client.py:235-296`,
  `.fetchall()` at `:274`). On large databases it can take many minutes.
- It runs in a worker thread (`DataSourceClient.aget_schemas` →
  `asyncio.to_thread`, `clients/base.py:219`), so the heartbeat keeps running.
- Verified with a throwaway test: real `refresh_schema` plus a fake client that
  blocks silently in a thread past the stale window. The run was not reaped
  and completed. The existing tests in
  `backend/tests/unit/test_connection_indexing_liveness.py` also pass.
- A quiet discovery would also be stopped by the idle timeout (900s, `:203`)
  with a different message ("Schema discovery made no progress…"), not this
  one.

## When the heartbeat stops

In each case below the run is lost, and about 10 minutes later the row shows
this error. For this MSSQL failure, check **out of memory** and **deploy or
rolling restart** first: the large one-shot catalog read makes memory the prime
suspect, and a long crawl makes it likely a restart lands mid-run.

### The process is killed

1. **Out of memory.** `_get_tables_enriched` loads every column of every
   table, plus `NVARCHAR(MAX)` table and column descriptions, in one
   `fetchall()`. It then builds `Table` objects, normalizes them, and upserts
   the whole catalog in one ORM session (`connection_service.py`
   `refresh_schema`). With up to 4 uvicorn workers per pod:
   - with a pod memory limit, Kubernetes kills the container (`OOMKilled`);
   - with no limit (the chart default, `k8s/chart/values.yaml:303`), the
     node's OOM killer can pick the process, or the kubelet evicts the pod
     under node memory pressure.
2. **Liveness probe failure.** `/health` with `timeoutSeconds: 1`,
   `failureThreshold: 3` (`k8s/chart/values.yaml:409`). Three slow answers in a
   row, for example under CPU throttling or with all workers busy, and the pod
   is restarted.
3. **uvicorn worker health check.** With `--workers > 1`, the uvicorn parent
   pings each worker and kills one that doesn't answer within 5s. Log line:
   `Child process [pid] died`.
4. **Native crash.** A segfault in pyodbc, msodbcsql or the Kerberos/GSSAPI
   libraries takes the whole worker down. Unlikely, but it looks the same from
   the outside.

### The pod is stopped on purpose

5. **Deploy or rolling restart.** `helm upgrade`, a new image, a config or
   secret change that rolls the pods, `kubectl rollout restart`. Every pod is
   replaced, so a crawl in progress during a deploy always dies.
6. **Scale-down.** With autoscaling on, a pod removed during scale-down takes
   its runs with it.
7. **Node maintenance or loss.** Node drain, cluster upgrade, a spot or
   preemptible node reclaimed, a node crash.
8. **Manual restart** or deleting the pod.

### The process is alive but the heartbeat stops

9. **Background loop frozen.** All indexing runs in one worker share one
   background event loop (`_get_background_loop`), and the heartbeat runs on
   it too. If any connector's code makes a blocking call directly on that loop
   instead of in a thread, every heartbeat in that worker stops, and runs that
   are still working get failed after 10 minutes. MSSQL itself doesn't do
   this; its query runs in a thread.
10. **Heartbeat can't reach the app database for 10+ minutes.** Examples: a
    Postgres outage or failover, network problems, expired IAM auth tokens.
    These write failures are only logged at debug level
    (`indexing.heartbeat_failed`), so they're invisible by default. Rare,
    because the progress updates would usually fail too.

## How to confirm

- `kubectl get pods`: RESTARTS count and pod age, compared to when the run
  started (the row's `started_at`).
- `kubectl describe pod <pod>`: look for `OOMKilled`, `Liveness probe failed`.
- Backend logs: `Child process [...] died`, `Waiting for child process`,
  `indexing.reaped_stale`.
- Also record the size of the MSSQL catalog: table and column counts in
  `INFORMATION_SCHEMA.COLUMNS` for the indexed schemas.

## Proposed fixes

### For the MSSQL failure

- Stream the catalog instead of loading it all at once: query one schema (or
  one batch of tables) at a time, iterate the cursor instead of calling
  `fetchall()`, and report progress per batch.
- Make the enriched query cheaper. It joins on
  `OBJECT_ID(c.table_schema + '.' + c.table_name)` for every row. Joining
  `sys.columns` / `sys.tables` / `sys.schemas` on ids instead would be much
  faster.
- Upsert the catalog in chunks, committing per chunk, to cap memory during
  the save step.

### Multi-replica issues found while diagnosing (independent of the MSSQL failure)

1. **Cancel only works on the same pod.** `_cancel_events` (`:47`) is held in
   process memory. A cancel request that reaches the other replica:
   - marks the row `cancelled` in the DB;
   - does not stop the crawl, which keeps running on the first pod;
   - lets the row be overwritten as `completed` at finalize (`:1033`, which
     reads through the runner's session without a refresh);
   - lets a new run start meanwhile, so two crawls hit the same source.

   Fix: have the runner re-read the row status at its checkpoints (the
   heartbeat is a natural place to do this) and set the local cancel event when
   the row is terminal. At finalize, do not overwrite a terminal status.
2. **Duplicate runs.** `start()` (`:460`) checks for an active run and then
   inserts, with no DB constraint. Two near-simultaneous requests on different
   replicas can create two active runs. Fix: a partial unique index on
   `(connection_id, user_id)` where status is pending/running (or a row lock),
   handling the conflict by returning the existing row.
3. **Concurrency limit is per worker.** `BOW_INDEXING_CONCURRENCY` (`:95`)
   applies to each uvicorn worker, not the whole deployment. Up to
   4 × workers × replicas crawls can run at once. Document this, or move the
   limit into the DB.
4. **No failover.** A run lost to a pod restart is only detected after 10
   minutes, and it is not resumed. A real fix needs a durable job queue, or
   resumable, checkpointed crawls.
