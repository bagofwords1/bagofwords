# AWS CloudWatch connector — reproduce → fix → verify

Adds `aws_cloudwatch`: CloudWatch **log groups** (queried with Logs Insights)
and **metrics** cataloged as tables on one connection.

Validated against a live AWS account (eu-west-1 + us-east-1) and driven
end-to-end through the real UI with a real LLM (Claude 4.5 Haiku).

## What shipped

| File | Why |
|---|---|
| `backend/app/data_sources/clients/aws_cloudwatch_client.py` | The client — discovery, Insights lifecycle, metrics, prompt |
| `backend/app/schemas/data_sources/configs.py` | `AWSCloudWatchConfig` + 3 credential variants (these *are* the form) |
| `backend/app/schemas/data_source_registry.py` | `REGISTRY["aws_cloudwatch"]`, `category="infra"`, `dev_only=True` |
| `backend/tests/unit/test_aws_cloudwatch_client.py` | 61 tests at the boto3 boundary |
| `frontend/public/data_sources_icons/aws_cloudwatch.png` | Brand icon (no `DataSourceIcon.vue` change — the resolver already falls back to `<type>.png`) |

No new dependency: `boto3` was already pinned, and `logs`/`cloudwatch` are core.

## Design decisions

**One type, two table namespaces.** `log_group::<name>` and
`metric::<Namespace>/<MetricName>`. An incident crosses both halves — the
Lambda erroring in Logs is the one whose Duration lives in Metrics — so
splitting them into two connection types would put them on separate
connections with no way for the planner to correlate. This mirrors how
`splunk_client.py` namespaces `index::sourcetype` alongside `dashboard::`.

**Logs discovery is schema-on-read.** CloudWatch has no field catalog, so a log
group's columns come from sampling recent events and parsing JSON out of
`@message`. Only the top-K groups by stored bytes are sampled; the rest stay
*thin* (the `@`-builtins only) and the agent peeks on demand. Safe because an
unknown field in Insights matches nothing rather than erroring.

**Auth mirrors S3/Athena**: static keys, keys + STS assume-role, or the default
chain (the one that matters for EKS IRSA, where no secret should be stored).

## What only real API contact revealed

These are the findings that changed the implementation. None were predictable
from the docs.

### 1. `limit` does not bound Insights cost

A `fields @timestamp, @message | limit 8` over three log groups reported:

```
recordsMatched: 31620   recordsScanned: 31620   bytesScanned: 131926304  (126 MB)
```

Insights bills on **bytes scanned**, and the `limit` clause does not reduce the
scan. Only the time window and the log-group selection do. Consequences:
`DEFAULT_QUERY_WINDOW` is a deliberate `-1h` (not the discovery window),
`max_sampled_log_groups` caps indexing cost, and `system_prompt()` states the
rule outright so the agent doesn't "optimize" by adding a limit.

### 2. `StopQuery` raises on an already-finished query

```
InvalidParameterException: Query is already ended with Complete
```

So the timeout path must cancel *and* swallow that error — while still
surfacing the `TimeoutError`. An abandoned Insights query keeps scanning, and
keeps billing, so not cancelling is not an option.
Pinned by `test_stop_query_failure_does_not_mask_the_timeout`.

### 3. The log-group-per-query cap is real

60 log groups → `InvalidParameterException: Too many log groups specified`.
Enforced client-side at 50 *before* the API call, so the failure is an
actionable `ValueError` instead of a wasted round trip.

### 4. `ListMetrics` returns one entry per dimension-set variant

`AWS/RDS/VolumeReadIOPs` comes back three times — for `[DBClusterIdentifier]`,
for `[DbClusterIdentifier, EngineName]`, and for `[]`. Entries are therefore
grouped by (namespace, metric) with their dimension names **unioned** into one
table's columns.

### 5. Insights always adds `@ptr`, and returns everything as strings

`@ptr` is an opaque cursor — dropped unless explicitly selected. `stats … by
bin(30m)` buckets come back as `"2026-09-01 18:30:00.000"` strings, which plot
as a *category* rather than a time axis, so they are parsed back to datetimes.

## The bug the LLM found

This is the one worth reading. Everything above passed unit tests and a live
client validation. Then the agent was asked, through the chat UI:

> whats cpu been doing on my ec2 boxes lately?

and produced:

```
Create Data  metric::AWS/EC2/CPUUtilization  9.7s
Execution error: "['InstanceId'] not in index"
```

then, on retry:

> The data is empty — there's no CPU utilization data in CloudWatch for your
> eu-west-1 region right now. […] Do you have EC2 instances running in eu-west-1?

Both statements were wrong, and both traced to the same defect:

1. **The catalog and the query result disagreed.** `get_schemas()` advertised
   `InstanceId` as a column of `metric::AWS/EC2/CPUUtilization`, but
   `_execute_metric` returned `series, timestamp, value`. Generated code did
   `df['InstanceId']` — exactly what the schema invited — and raised `KeyError`.
2. **A dimensionless metric query returns nothing.** `AWS/EC2 CPUUtilization`
   only exists *per InstanceId*; a `MetricStat` with empty `Dimensions` matches
   no series. The empty frame then read to the agent as "you have no instances"
   — a confidently wrong answer about live infrastructure.

**Fix.** `_execute_metric` now resolves a metric's real dimension sets via
`ListMetrics` when none are supplied, issues one query per set, and populates
the dimension **columns** the catalog promised. Verified against live AWS:

```
before:  shape=(0, 3)   columns=['series','timestamp','value']
after:   shape=(72, 4)  columns=['InstanceId','series','timestamp','value']
         72 rows across 3 distinct EC2 instances
```

Pinned by four tests, including
`test_metric_result_carries_the_dimension_columns_the_catalog_advertises`,
which asserts the catalog and the result frame agree — the invariant that was
actually violated, not the anecdote.

**Lesson for the next connector:** a table-shaped client has a contract between
`get_schemas()` and `execute_query()`. Unit tests that mock the driver verify
each half in isolation and will not catch a disagreement between them. Only a
real planner writing real code against the real catalog did.

## Reproduce

```bash
cd backend
uv sync --extra dev
BOW_DATABASE_URL='sqlite:///db/app.db' uv run alembic upgrade head

# Registry resolves + client imports via client_path
BOW_DATABASE_URL='sqlite:///db/app.db' uv run python -c \
  "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('aws_cloudwatch'))"

TESTING=true uv run pytest tests/unit/test_aws_cloudwatch_client.py -q
TESTING=true uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
```

Live pass (needs read-only CloudWatch credentials in the environment):

1. `tools/agent/boot_stack.sh`, sign up, add an LLM provider.
2. **Agents → new → Infrastructure → AWS CloudWatch.** Fill Region, optionally
   Log Group Prefix / Metric Namespaces, pick **AWS Access Key**.
3. **Test connection** → `Connected successfully. Found N tables.`
4. Save → schema discovery runs → tables appear as `metric::…` / `log_group::…`.
5. **Activate the tables** (the Select Tables step). An agent created through
   the API leaves `datasource_tables.is_active = 0`, and the planner then
   reports the agent "doesn't expose queryable data" — see below.
6. Ask something vague ("whats cpu been doing on my ec2 boxes lately?").

## Sandbox gotchas hit along the way

- **`main.py` hardcodes `reload=True`**, and the reload worker did not inherit
  the exported `BOW_ENCRYPTION_KEY`. The worker then invented its own key, which
  invalidated every JWT (same secret) — Playwright `storageState` started
  returning 401 mid-run. Fix: run `uvicorn main:app` directly (no reloader) with
  the env applied to the process.
- The auth token is the **`auth.token` cookie** (`@sidebase/nuxt-auth`,
  `signInResponseTokenPointer: /access_token`). A raw `fetch('/api/…')` from
  `page.evaluate` returns 401 because the app attaches `Authorization: Bearer`
  itself — that 401 is *not* evidence of a broken session.
- Uvicorn hung on `Waiting for background tasks to complete` after serving
  chats; the port stayed held and the replacement never bound. `kill -9` the
  tree, leave the WAL alone.
- `connection_service.construct_client` passes **`auth_type`** through to the
  client constructor (unlike `Connection.get_client()`, which strips it). A
  client without `**kwargs` would `TypeError` on this path.
- An agent created via `POST /api/data_sources` links its tables but leaves them
  **inactive**; activate with
  `PUT /api/data_sources/{id}/update_tables_status`.

## Region note

The demo account has **no log groups in eu-west-1** (metrics only). The Logs
half was validated against `us-east-1`, which has four real log groups —
including `RDSOSMetrics`, whose JSON bodies exercised the field sampler:

```
log_group::RDSOSMetrics  →  @timestamp, @message, @logStream, @ingestionTime,
                            acuUtilization, cpuUtilization, diskIO, engine,
                            fileSys, instanceID, loadAverageMinute, memory, …
```

`/ecs/hiai-task-definition` is plain-text, so it correctly stayed thin.

## Follow-ups

- Extract a shared `clients/_aws_session.py` — S3, Athena and CloudWatch now
  carry three copies of the keys/assume-role/default-chain branch.
- Consider a `filter_log_events` fast path: a plain "tail the last N errors from
  one group" is free, where Insights costs per GB scanned.
- `requires_license` is currently unset (matching Prometheus). Splunk is
  `enterprise`; if CloudWatch should be gated the same way, that is a one-line
  registry change.
