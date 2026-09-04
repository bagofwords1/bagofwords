# AWS CloudWatch — Connector Design

**Status: design only. Nothing is implemented.** This doc answers "how should an
AWS CloudWatch connector work?" — which CloudWatch APIs are worth connecting,
how metrics *and* logs fit one connection, how it maps onto our registry-driven
connector pattern, and what to build in what order.

Date: 2026-09-04. Companion docs with the same shape:
`docs/cloudflare-connector-analysis.md`,
`docs/vmware-aria-storage-connectors-analysis.md`,
`docs/priority-erp-connector-analysis.md`.

---

## 0. Bottom line up front

1. **Build one connector, `aws_cloudwatch`, covering Metrics + Logs + Alarms —
   not two.** They are different AWS APIs (`cloudwatch` vs `logs`) but they are
   one *connection*: one AWS account, one region, one IAM principal. Every
   real question this connector exists for ("latency spiked at 14:05 — what do
   the logs say?") crosses both. Splitting them forces the agent to join across
   two connections that share an identity, for no benefit.
2. **Zero new dependencies.** `boto3>=1.43.33` is already a backend dependency
   (`backend/pyproject.toml:51`) and three of our connectors already use it
   (`s3`, `aws_athena`, `aws_cost`).
3. **Reuse the S3 client's three auth variants verbatim** — `key` (static
   access/secret), `role` (STS assume-role, keys optional so an instance
   profile / EKS IRSA can assume it), `default` (boto3's own chain). That
   pattern is already written and already reviewed
   (`backend/app/schemas/data_sources/configs.py:2424`,
   `backend/app/data_sources/clients/s3_client.py:233`). Do not invent a fourth
   shape.
4. **Catalog shape: one table per `(namespace, metric_name)` for metrics, one
   table per log group for logs, one fixed `alarms` table.** `ListMetrics`
   returns each metric with its dimension sets, and `GetLogGroupFields` returns
   a log group's discovered field names — so both families get **accurate
   columns**, not guesses. This is the `prometheus_client.py` model (one table
   per metric, columns = labels + `timestamp` + `value`) applied to CloudWatch.
5. **Query dispatch is a JSON envelope, like `splunk_client.execute_query`.**
   Four query kinds behind one method: Metrics Insights SQL, an explicit metric
   query (for windows Metrics Insights cannot reach), Logs Insights, and alarm
   state. A bare string is accepted with a documented heuristic, but the
   envelope is the contract.
6. **Cost and quota guardrails are a design requirement, not polish.**
   CloudWatch bills per API call and Logs Insights bills **per GB scanned** — an
   agent that writes an unbounded `fields @message` over 90 days of a busy log
   group produces a real invoice. Mandatory time window, mandatory `limit`, a
   configured log-group allowlist, and a scanned-bytes readback belong in v1.
   See §6.
7. **The two window limits will bite the agent unless they are in the prompt.**
   Metrics Insights SQL reaches **two weeks** back (raised from three hours) and
   returns **at most 500 time series**; raw metric queries reach far further but
   with period-dependent resolution rules. The client must pick the right API
   for the requested window rather than letting the agent fail into a wall.

---

## 1. What CloudWatch exposes, and what we take

| API | Call | What it gives | Verdict |
|---|---|---|---|
| **Metrics — Insights SQL** | `GetMetricData` with `Expression` | `SELECT AVG(m) FROM SCHEMA(ns, dim) WHERE … GROUP BY … ORDER BY … LIMIT …` over *dynamic* metric sets | **Primary metrics path.** Top-N and group-by without enumerating resources. |
| **Metrics — explicit** | `GetMetricData` with `MetricStat` | Named metric + dimensions + statistic + period | **Also needed** — the only way past the 2-week Metrics Insights window. |
| **Metrics — catalog** | `ListMetrics` | Every metric name + its dimension sets, paginated; `RecentlyActive=PT3H` scopes to live metrics | **Catalog source.** |
| **Logs — search** | `StartQuery` → poll `GetQueryResults` | Async query over log groups; Logs Insights QL, OpenSearch PPL, or OpenSearch SQL | **Primary logs path.** |
| **Logs — catalog** | `DescribeLogGroups`, `GetLogGroupFields` | Log groups + their discovered fields with occurrence percentages | **Catalog source.** |
| **Alarms** | `DescribeAlarms`, `DescribeAlarmHistory` | Current state + state transitions | **One fixed table each.** Cheap, and it is what an RCA question opens with. |
| Metric streams / Contributor Insights / Synthetics | — | Niche | Skip v1. |
| `GetMetricStatistics` | — | Superseded by `GetMetricData` | Skip. |

---

## 2. Auth + config

```python
# backend/app/schemas/data_sources/configs.py

class CloudWatchKeyCredentials(BaseModel):
    access_key: str
    secret_key: str                 # ui:type=password
    session_token: Optional[str]    # ui:type=password — for temporary STS creds

class CloudWatchRoleCredentials(BaseModel):
    role_arn: str
    access_key: Optional[str]       # blank -> assume via instance profile / IRSA
    secret_key: Optional[str]       # ui:type=password
    external_id: Optional[str]      # third-party / cross-account trust

class CloudWatchDefaultCredentials(BaseModel):
    """No credentials — boto3's default chain (env, shared config, instance
    profile, IRSA). Mirrors S3DefaultCredentials / AWSAthenaDefaultCredentials."""
    class Config:
        extra = "allow"

class CloudWatchConfig(BaseModel):
    region: str                              # e.g. "us-east-1"
    namespaces: Optional[str]                # comma-separated allowlist, e.g. "AWS/EC2,AWS/RDS,MyApp"
    log_group_prefix: Optional[str]          # scope log-group discovery, e.g. "/aws/lambda/prod-"
    log_groups: Optional[str]                # explicit comma-separated allowlist; wins over prefix
    recently_active_only: bool = True        # ListMetrics RecentlyActive=PT3H
    max_metric_tables: int = 2000            # catalog cap
    default_lookback_hours: int = 24
    default_period_seconds: int = 300
    max_rows: int = 10000
    monitoring_account: bool = False         # cross-account observability: include linked accounts
    endpoint_url: Optional[str]              # LocalStack / VPC endpoints / GovCloud
```

Registry entry (`backend/app/schemas/data_source_registry.py`):

```python
"aws_cloudwatch": DataSourceRegistryEntry(
    type="aws_cloudwatch",
    category="infra",              # alongside prometheus / splunk / jaeger / aws_cost
    title="AWS CloudWatch",
    description="Metrics, logs and alarms from Amazon CloudWatch — query metrics with "
                "Metrics Insights SQL, search logs with Logs Insights, and read alarm "
                "state across one AWS account and region.",
    config_schema=CloudWatchConfig,
    credentials_auth=AuthOptions(default="key", by_auth={
        "key":     AuthVariant(title="AWS Keys", schema=CloudWatchKeyCredentials, scopes=["system", "user"]),
        "role":    AuthVariant(title="Assume Role (STS)", schema=CloudWatchRoleCredentials, scopes=["system"]),
        "default": AuthVariant(title="Instance Profile / IRSA", schema=CloudWatchDefaultCredentials, scopes=["system"]),
    }),
    client_path="app.data_sources.clients.aws_cloudwatch_client.CloudWatchClient",
    version="beta",
),
```

`data_shape="tables"`, `catalog_ownership="shared"`, `ui_form="data_source"` —
all defaults. `client_path` explicit, per the **add-connection-type** skill's
first pitfall.

**One connection = one region.** Multi-region estates create one connection per
region, which is also how IAM and cross-region latency actually work. The
alternative — a region list on one connection — makes every table name carry a
region prefix and every query fan out; not worth it in v1.

**Least-privilege IAM** to put in the field description and the docs page:
`cloudwatch:ListMetrics`, `cloudwatch:GetMetricData`, `cloudwatch:DescribeAlarms`,
`cloudwatch:DescribeAlarmHistory`, `logs:DescribeLogGroups`,
`logs:GetLogGroupFields`, `logs:StartQuery`, `logs:GetQueryResults`,
`logs:StopQuery`. All read-only. Nothing here mutates.

---

## 3. Catalog — how `get_schemas()` builds tables

### Metrics: one table per `(namespace, metric_name)`

`ListMetrics` (paginated, `RecentlyActive="PT3H"` when
`recently_active_only`, filtered by the `namespaces` allowlist) yields metrics
with their dimension sets. Group by `(Namespace, MetricName)`:

```
name:    AWS/EC2.CPUUtilization
columns: InstanceId (dimension), InstanceType (dimension), AutoScalingGroupName (dimension),
         timestamp (datetime), value (double), stat (label)
metadata_json: {"namespace": "AWS/EC2", "metric_name": "CPUUtilization",
                "dimension_sets": [["InstanceId"], ["AutoScalingGroupName"], []],
                "source": "cloudwatch_metrics"}
```

`dimension_sets` matters and must not be collapsed into a flat column list:
Metrics Insights' `SCHEMA(ns, dimA, dimB)` matches an **exact** dimension set,
so the agent needs to know which combinations actually exist. Render them into
the table description.

`stat` is a synthetic column, not a dimension — it records which statistic
(`Average`/`Sum`/`Maximum`/…) produced each row, so a DataFrame that mixes
statistics stays honest.

Cap at `max_metric_tables`; a busy account with custom metrics can otherwise
produce tens of thousands of tables. When the cap trips, index the most
recently active first and say so in the connection's status message rather
than silently truncating.

### Logs: one table per log group

`DescribeLogGroups` (filtered by `log_groups` / `log_group_prefix`), then
`GetLogGroupFields` per group for real columns:

```
name:    logs./aws/lambda/prod-checkout
columns: @timestamp (datetime), @message (string), @logStream (string), @requestId (string),
         level (string), duration_ms (double), …   # discovered fields
metadata_json: {"log_group": "/aws/lambda/prod-checkout", "retention_days": 30,
                "stored_bytes": 41231234, "source": "cloudwatch_logs"}
```

`GetLogGroupFields` returns each field with the percentage of events containing
it — keep that in `metadata_json` and surface the top fields in the description,
so the agent filters on fields that are actually present.

Carry `retention_days` and `stored_bytes` into the description too: they are the
agent's only signal for "is a 30-day scan of this group reasonable?".

### Alarms: two fixed tables

- `cloudwatch.alarms` — `alarm_name`, `state_value`, `state_reason`,
  `state_updated_at`, `namespace`, `metric_name`, `threshold`, `comparison`,
  `actions_enabled`.
- `cloudwatch.alarm_history` — `alarm_name`, `timestamp`, `history_item_type`,
  `summary`.

Both from `DescribeAlarms` / `DescribeAlarmHistory` at query time (never cached
into the catalog — alarm state is the one thing that must be live).

Support `progress_callback`: `GetLogGroupFields` is one call per log group, so
an estate with 400 Lambda functions makes indexing visibly slow. The base
inspects `get_schemas` for the kwarg.

---

## 4. Query contract

One `execute_query` with a JSON envelope, dispatched on the key present —
directly modelled on `splunk_client.execute_query`
(`backend/app/data_sources/clients/splunk_client.py:766`), which already proved
this shape works with the coder agent.

```python
# 1. Metrics Insights SQL — dynamic metric sets, top-N, group-by. Window <= 2 weeks.
client.execute_query({
    "metrics_insights": """
        SELECT AVG(CPUUtilization) FROM SCHEMA("AWS/EC2", InstanceId)
        GROUP BY InstanceId ORDER BY MAX() DESC LIMIT 10
    """,
    "start": "2026-09-01T00:00:00Z", "end": "2026-09-04T00:00:00Z",
    "period": 300,
})

# 2. Explicit metric query — the only path past 2 weeks (up to 15 months).
client.execute_query({
    "metric": {"namespace": "AWS/RDS", "metric_name": "DatabaseConnections",
               "dimensions": {"DBInstanceIdentifier": "prod-1"},
               "stat": "Average"},
    "start": "2026-03-01T00:00:00Z", "end": "2026-09-01T00:00:00Z",
    "period": 3600,
})

# 3. Logs Insights — async StartQuery + poll.
client.execute_query({
    "logs_insights": "fields @timestamp, @message | filter level = 'ERROR' "
                     "| stats count() by bin(5m)",
    "log_groups": ["/aws/lambda/prod-checkout"],
    "start": "-6h", "end": "now", "limit": 1000,
    "language": "CWLI",          # CWLI (default) | PPL | SQL
})

# 4. Alarms.
client.execute_query({"alarms": {"state": "ALARM"}})
client.execute_query({"alarm_history": {"alarm_name": "prod-5xx", "start": "-7d"}})
```

Every kind returns a flat `pandas.DataFrame` whose columns match the catalog:
metrics → `timestamp`, `value`, `stat`, one column per grouped dimension label;
logs → the field names the query projected; alarms → the fixed columns above.

**Bare-string fallback** (documented, not preferred): a string starting with
`SELECT` and containing `FROM` with a CloudWatch namespace or `SCHEMA(` → Metrics
Insights; a string starting with `fields`/`filter`/`stats` or containing `|` →
Logs Insights QL. Anything else raises with a message naming the four envelope
keys. The ambiguity is real — OpenSearch SQL over logs also starts with
`SELECT` — which is exactly why the envelope is the contract and the string is
the fallback.

### Client-side behaviour that has to be right

- **API selection by window.** If a `metrics_insights` request spans more than
  ~14 days, the client should either raise with a message that names the
  explicit-metric path, or transparently rewrite simple single-metric queries
  into a `MetricStat` query. Raising with the right suggestion is the safer v1;
  the agent's retry loop then repairs it.
- **Period vs retention.** 1-minute data is retained 15 days, 5-minute 63 days,
  1-hour 455 days. Auto-select the coarsest period that covers the requested
  window when the caller omits `period`, and say which was used.
- **Logs Insights is asynchronous.** `StartQuery` → poll `GetQueryResults` with
  backoff → `StopQuery` on cancellation. Wire the poll loop to the existing
  `CancelCheck` / `ProgressCallback` plumbing (`clients/progress.py`) so a
  60-second log query is cancellable and shows progress, not a frozen spinner.
- **Concurrency.** CloudWatch Logs caps concurrent Insights queries per account
  (on the order of 30). A 429/`LimitExceededException` must surface as a typed
  retryable error, not a stack trace — and the existing
  `data_sources/query_concurrency.py` guard should cap our own share so we never
  starve the customer's own dashboards.
- **Pagination.** `GetMetricData` returns 500 metrics per request with a
  `NextToken`; `GetQueryResults` caps at 10,000 rows (100,000 only for Logs
  Insights QL, not PPL/SQL). Page to `max_rows`, then stop and mark the result
  truncated rather than silently returning a partial answer as if it were
  complete.
- **Relative-time shorthand.** Accept `-6h` / `-7d` / `now` in `start`/`end`
  and resolve them **client-side at execution time**. This is what keeps a
  saved dashboard correct on tomorrow's refresh.

`relative_date_hint`:

> *"No relative-date functions in the query language — pass `start`/`end` in the
> query envelope, either as ISO-8601 or as `-24h`/`-7d`/`now` shorthand which the
> client resolves at execution time. Never bake absolute dates into a saved
> query."*

`test_connection()` should call `ListMetrics` with a `MaxRecords`-shaped bound
(cheap, no data charge) and report the region plus the caller identity from
`sts:GetCallerIdentity` — "connected as `arn:aws:iam::…:role/bow-readonly` in
`us-east-1`" is the message that actually helps an admin debug an IAM mistake.

---

## 5. What the agent is told (`description`)

Same pattern as Prometheus: instance identity + full usage guide, since
`description` is all the coder agent sees (`render_ds_client_entry`,
`backend/app/ai/prompt_formatters.py:6`). It must state:

- Not SQL over a database — four query kinds behind one envelope.
- **Metrics Insights syntax rules that generate silent failures:** keywords are
  case-insensitive but metric names, namespaces and dimensions are
  **case-sensitive**; namespaces containing `/` need **double** quotes
  (`FROM "AWS/EC2"`); label values need **single** quotes; `SCHEMA(ns, dims…)`
  matches an *exact* dimension set, while bare `FROM ns` matches any.
- `ORDER BY FUNC() DESC` + `LIMIT n` is the top-N idiom; max 500 time series
  returned whether or not `LIMIT` is given.
- Metrics Insights reaches 2 weeks; use the `metric` envelope for anything older.
- Logs Insights costs money per GB scanned — always narrow the time range and
  the log-group list first, and prefer `stats`/`filter` over raw `@message`
  dumps.
- Alarm state answers "what is broken right now" in one cheap call; start RCA
  there before scanning logs.

---

## 6. Cost and blast-radius guardrails

This is the connector where an unhelpful design costs the customer money.

| Risk | Guardrail |
|---|---|
| Unbounded Logs Insights scan | Mandatory time window (`default_lookback_hours` injected when absent); mandatory `limit`; `log_groups`/`log_group_prefix` allowlist enforced client-side so a query cannot reach a group outside the connection's scope |
| Scanned-bytes surprise | `GetQueryResults` returns `Statistics.bytesScanned` — log it, and include it in the result metadata so cost shows up in the report, not the invoice |
| Catalog indexing cost | `ListMetrics`, `DescribeLogGroups`, `GetLogGroupFields` are the cheap calls; `recently_active_only` and `max_metric_tables` bound the sweep. Do **not** probe data during indexing |
| Concurrent-query starvation | Cap our own in-flight Insights queries via `query_concurrency.py`; surface `LimitExceededException` as retryable |
| Cross-account overreach | `monitoring_account` is opt-in; when off, never pass `includeLinkedAccounts` / `AWS.AccountId` filters |

---

## 7. Files to touch

| Layer | File | Change |
|---|---|---|
| Client | `backend/app/data_sources/clients/aws_cloudwatch_client.py` | New `CloudWatchClient(DataSourceClient)` |
| Config | `backend/app/schemas/data_sources/configs.py` | `CloudWatchConfig` + three credentials classes |
| Registry | `backend/app/schemas/data_source_registry.py` | Import the four + add the `"aws_cloudwatch"` entry |
| Icon | `frontend/public/data_sources_icons/aws_cloudwatch.png` | CloudWatch mark (the default `<type>.png` resolver picks it up — no `DataSourceIcon.vue` edit needed for a PNG) |
| Tests | `backend/tests/unit/test_aws_cloudwatch_client.py` | Fake boto3 clients injected at the `_client()` boundary, as `test_s3_client.py` does |
| Tests | `backend/tests/integrations/ds_clients.py` | `"aws_cloudwatch"` — LocalStack container if it covers enough, else remote mode via `integrations.json` |
| Docs | `docs/feedback-loops/aws-cloudwatch-connector.md` | The reproduce→fix→verify loop (**sandbox-feedback-loop** skill) |

No new dependency, no frontend form work.

---

## 8. Testing

- **Unit (no AWS).** Inject fake `cloudwatch` / `logs` / `sts` clients at the
  `_client()` boundary — mock the **driver boundary only**, per
  `backend/tests/AGENTS.md`. Cover: `ListMetrics` pagination → table set and the
  `max_metric_tables` cap; `dimension_sets` preserved distinctly (not flattened);
  `GetLogGroupFields` → log-group columns; envelope dispatch across all four
  kinds + both bare-string heuristics; `-6h`/`now` resolution; auto period
  selection per window; `GetMetricData` `NextToken` paging and truncation
  marking; the Logs Insights poll loop honouring `CancelCheck` and calling
  `StopQuery`; `LimitExceededException` surfacing as retryable; log-group
  allowlist enforcement rejecting an out-of-scope group; all three auth variants
  building the right boto3 session (assume-role path included);
  `resolve_client_class("aws_cloudwatch")`.
- **Integration.** LocalStack (community) covers `cloudwatch` and `logs`
  well enough for catalog + basic query paths, and is the only option that runs
  in CI without live credentials — add a `CONTAINER_REGISTRY` entry if it holds
  up. Metrics Insights SQL support in LocalStack should be verified early; if it
  is missing, that path falls back to remote mode against a real account
  (`integrations.json`, never committed).
- **Live UI pass.** `tools/agent/boot_stack.sh` + `seed_org.py`; create the
  connection → Test connection reports the caller ARN → Tables Selector shows
  `AWS/*.*`, `logs.*`, `cloudwatch.alarms` → prompt "which instances had the
  highest CPU in the last 6 hours, and what errors were in the checkout Lambda
  logs at that time" and confirm the agent uses Metrics Insights for the first
  half and Logs Insights for the second. Screenshots via **ui-evidence**.

---

## 9. Build order

1. **v1a — metrics.** `ListMetrics` catalog, Metrics Insights + explicit metric
   queries, `alarms`/`alarm_history`, all three auth variants, `dev_only=True`.
   This alone is a useful connector and is the smaller half.
2. **v1b — logs.** Log-group catalog, async Logs Insights with cancellation and
   progress, cost guardrails, `bytesScanned` readback.
3. **v1.1** — cross-account (`monitoring_account`), OpenSearch PPL/SQL language
   selection, drop `dev_only`.
4. **v2 (only if asked)** — Contributor Insights, metric math expressions
   beyond Metrics Insights, Synthetics canaries.

---

## 10. Open questions to settle during the build

- **Metrics catalog granularity.** One table per `(namespace, metric_name)` is
  the Prometheus-consistent choice and gives exact dimensions, but a
  custom-metric-heavy account may blow past `max_metric_tables`. The fallback is
  one table per namespace with `metric_name` as a column — decide against a real
  account before dropping `dev_only`, and keep the cap behaviour visible in the
  connection status either way.
- **LocalStack fidelity** for Metrics Insights SQL and `GetLogGroupFields`. If
  it is thin, CI covers unit + catalog only and the query paths stay remote-mode.
- **Auto-rewriting long Metrics Insights windows** into explicit metric queries:
  convenient, but it silently changes semantics for `GROUP BY` queries. v1
  raises with a suggestion; revisit once there is evidence the agent's retry
  loop handles it well.
- **Log-group fan-out.** `StartQuery` accepts multiple log groups, but scanning
  400 Lambda groups is expensive. Consider a per-query cap on log-group count
  with an explicit override.

## Sources

- [CloudWatch Metrics Insights query syntax](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/cloudwatch-metrics-insights-querylanguage.html)
- [Metrics Insights now queries up to two weeks of data](https://aws.amazon.com/about-aws/whats-new/2025/09/amazon-cloudwatch-query-metrics-data-two-weeks)
- [CloudWatch Logs `StartQuery`](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_StartQuery.html)
- [CloudWatch Logs Insights query languages (QL, PPL, SQL)](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/CWL_AnalyzeLogData_LogsInsights.html)
- [`GetMetricData`](https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/API_GetMetricData.html)
