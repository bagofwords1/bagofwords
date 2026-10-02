# Plan: Datadog connector — `DatadogClient`

## Mission

Add a data source type `datadog` that lets the agent **query** a Datadog org —
logs, metrics, infrastructure inventory (hosts, cloud resources, k8s) and
cloud cost — through **DDSQL**, and get rows back as a DataFrame, with
`get_schemas` building a catalog of DDSQL tables plus the knowledge layer
(dashboards, monitors) an operator uses during RCA.

This follows `.agents/skills/add-connection-type/SKILL.md`. It is the native
complement to a (separate, later) Datadog **MCP preset**: the preset gives
tools; this gives an indexed catalog, `execute_query`, and tracked reports.

All "verified" facts below come from `tools/datadog/spike_ddsql.py` run
against a live trial org (US1, 2026-10-02) seeded with
`tools/datadog/seed_datadog.py`. Re-run both to re-verify.

## Query surface — DDSQL (verified)

`POST /api/v2/ddsql/query/tabular` → poll `POST /api/v2/ddsql/query/tabular/fetch`.

```jsonc
// submit
{"data": {"type": "ddsql_query_request", "attributes": {
  "query": "SELECT …", "row_limit": 1000,            // 1..10000
  "time": {"from_timestamp": <ms>, "to_timestamp": <ms>}}}}
// fetch
{"data": {"type": "ddsql_query_fetch_request", "attributes": {"query_id": "…"}}}
// response
{"data": {"attributes": {"state": "running|completed", "query_id": "…",
  "columns": [{"name": "service", "type": "VARCHAR", "values": [...]}],  // column-major
  "warnings": [...]}}, "meta": {"elapsed": 292, "request_id": ""}}
```

| Finding (spike) | Consequence for the client |
|---|---|
| Every query, incl. logs over 6h, came back `completed` on the first call in 0.4–0.8 s — no polling observed | Still implement the poll loop (state `running` is documented); bound it by a deadline. |
| Result is **column-major**; types seen: `BIGINT`, `DECIMAL`, `VARCHAR`, `VARCHAR[]`, `TIMESTAMP` (epoch **ms** ints), `HSTORE` (dict), `JSON` | One `_to_dataframe(columns)` — zip columns, convert `TIMESTAMP` from ms, keep arrays/dicts as objects. |
| `row_limit` **truncates silently** — 4470 matching rows with `row_limit=100` returned exactly 100, no warning | Request `row_limit = limit + 1`; if you get `limit + 1`, drop one and flag the result truncated. |
| Syntax/unknown-table errors are HTTP 400 with a precise `errors[].detail` (`depends on non-existent dataset "x"`, `mismatched input 'SHOW'`, `unsupported output column type 'DOUBLE'…`) | Surface `detail` verbatim to the agent — it is self-correcting material. |
| `AS (…)` column types for table functions accept only `bigint, boolean, decimal, json, timestamp, varchar` | Put this list in `system_prompt()` — `DOUBLE`/`INT` is the most likely LLM mistake. |
| Joins across a resource table and a table-function subquery work | No client-side join logic needed. |
| `dd.spans(...)` exists and returns typed empty results when no APM data | Catalog it; the agent gets 0 rows, not an error, on orgs without APM. |

### Table functions (verified against seeded data)

```sql
-- logs: columns use @ for custom attributes; output types are mandatory
SELECT service, count(*) AS errors
FROM dd.logs(filter => 'env:bow-demo status:error', columns => ARRAY['service'])
  AS (service VARCHAR)
GROUP BY service ORDER BY errors DESC;

-- metrics: Datadog metric query syntax inside the string
SELECT * FROM dd.metrics_timeseries('avg:bow.demo.request.latency.p95{env:bow-demo} by {service}');
SELECT * FROM dd.metrics_scalar('sum:bow.demo.request.errors{env:bow-demo} by {service}', 'sum');
```

Metrics functions return `timestamp`, the group-by keys as **`VARCHAR[]`**
(e.g. `service = ['checkout']`), `value DECIMAL`, `tags HSTORE`. The rollup is
chosen by Datadog from the window (10-min points over 6h). Also documented,
not spiked: `dd.cloud_cost_scalar/_timeseries` (24–48h delay), `dd.logs(indexes=>, storage=>)`.

## Catalog (`get_schemas`) — "everything in Datadog"

> **Update (2026-10-02, supersedes the build-time scraper below).** There is no
> SQL-level `information_schema`, but Datadog's **MCP server exposes the DDSQL
> schema catalog** at runtime (`?toolsets=ddsql`, API+app key headers, needs the
> `mcp_read` permission — a key without it gets 403 on tool calls):
>
> | MCP tool | Verified result |
> |---|---|
> | `ddsql_schema_search_tables(query=".", public_limit≤100, public_offset)` | **2,204 public tables** (+ reference tables, published analyses) and **277 org metrics**, paginated by 100 → ~26 calls for everything; each row has `name, id, searchable(=table function), description, ptf_format_doc` (exact call shape) |
> | `ddsql_schema_get_table_columns(table_id)` | typed columns, e.g. `public.k8s.pods` → 15 cols; for `metrics.<name>` → its tag keys |
> | `ddsql_schema_search_unstructured_fields(source_id="public.dd.logs")` | **org-specific** log fields with types (42 here, incl. seeded `@error.kind`, `@http.status_code`) — solves open item 3 |
> | `ddsql_get_spec` | DDSQL dialect deltas vs PostgreSQL → `system_prompt()` |
>
> So `get_schemas` = list all tables via MCP (cheap) → columns via MCP for
> tables with data (batched DDSQL `UNION ALL count(*)`) and for table functions →
> remaining tables stay **thin** (name + description; columns fetched on demand),
> the Splunk long-tail pattern. Don't fetch columns for all 2,204 on every
> reindex: MCP fair use is 50 calls/10 s and 100k calls/month. Queries keep
> going through REST DDSQL (60 req/20 s). The MCP path is `/api/unstable/…`.

Goal: one connection scans **all** data DDSQL can reach — k8s, hosts,
containers, cloud resources, DBM, services, monitors, events, logs, spans, RUM,
security, CI, network, LLM obs, cost.

### What exists (verified 2026-10-02)

The public [Data Directory](https://docs.datadoghq.com/ddsql_reference/data_directory/)
lists **2,173 datasets**: `aws.*` 1,258 · `gcp.*` 484 · `azure.*` 329 · `oci.*` 49 ·
`dd.*` 45 · `k8s.*` 8 (`clusters, daemonsets, deployments, namespaces, nodes,
pods, services, statefulsets`). They come in two kinds:

| Kind | Examples | How to query | Verified |
|---|---|---|---|
| **Static tables** (inventory/state) | `k8s.pods`, `dd.hosts`, `dd.containers`, `dd.services`, `dd.datadog_agents`, `dd.postgres_tables`, `aws.ec2_instance`, … | plain `SELECT … FROM k8s.pods` | all 8 `k8s.*`, 30 `dd.*`, 15 random cloud tables resolve |
| **Table functions** (event streams) | `dd.logs`, `dd.spans`, `dd.rum`, `dd.events`, `dd.audit`, `dd.monitors`, `dd.network`, `dd.network_device_flows`, `dd.llm_observability`, `dd.product_analytics`, `dd.security_findings`, `dd.ci_pipelines`, `dd.ci_tests`, `dd.monitor_groups`(?), `dd.metrics_*`, `dd.cloud_cost_*` | `FROM dd.x(columns => ARRAY[...], filter => '…') AS (…)` | `rum, events, audit, network, llm_observability, product_analytics, security_findings, ci_pipelines, monitors` all 200 as functions; plain `SELECT` on them is 400 "non-existent dataset" |

So **monitors and events are in DDSQL too** (as functions) — the REST
side-path is only needed for dashboards (widget queries), SLOs, incidents.

### Discovery constraints (verified)

* **No introspection** (`information_schema`, `pg_catalog`, `SHOW` → 400).
* **Rate limit: 60 DDSQL requests / 20 s** (`x-ratelimit-name:
  logs_advanced_query_api_query`, likely per org and shared with the DDSQL
  Editor). A naive per-table probe of 2,173 tables hit 429 after ~75 calls.
* `SELECT * FROM t LIMIT 0` returns typed columns for an empty table.
* `UNION ALL` of `SELECT 't' AS t, count(*) FROM t` works across tables in
  **one request** — 28 tables (k8s + dd + aws/gcp/azure) counted in one call.
  One missing dataset fails the whole batch with a 400 naming it, so the loop
  is: drop the named table, resubmit (3 requests for that batch).
* Every Data Directory page documents each field (name, data type, description)
  — e.g. `k8s.pods`: `_key, annotations(hstore), cluster_name, creation_timestamp,
  labels(hstore), name, namespace, spec_node_name, …`.

### Algorithm

1. **Build-time catalog** — `tools/datadog/build_ddsql_catalog.py` scrapes the
   Data Directory index + each dataset page → `datadog_ddsql_catalog.json`
   (table, kind static|function, fields with type + description, product/namespace).
   Ships with the client; regenerate per release. Gives rich column
   descriptions at zero API cost.
2. **Runtime — which tables have data**: batched `UNION ALL count(*)` over the
   static tables, ~50 per request, with drop-and-retry on "non-existent dataset"
   → ~45–60 requests for all 2,173, paced under the 60/20 s limit
   (≈ 20–30 s). Keep tables with `n > 0` (config `only_tables_with_data`,
   default on); a fresh trial org keeps ~5 (`dd.hosts`, `dd.agent_hosts`,
   `dd.datadog_agents`, `dd.datadog_agent_integrations`, …) — k8s/aws tables
   exist but are empty until a cluster/cloud account reports.
3. **Column truth for kept tables**: `LIMIT 0` only for kept tables (catches
   schema drift vs the shipped JSON).
4. **Function tables**: always catalogued with their documented fields; a cheap
   `LIMIT 1` per function over the default window tells "has recent data"
   (e.g. `dd.events` 5 rows, `dd.audit` 1, `dd.rum` 0 in the trial org).
   `dd.logs` additionally gets top facets (open item 3).
5. **Metrics**: names from `GET /api/v2/metrics` embedded in the
   `dd.metrics_*` descriptions; no table per metric.
6. **Knowledge tables** over REST: `dashboard::<title>` (widget queries),
   SLOs, incidents.
7. **One shared rate limiter** in the client (token bucket seeded from
   `x-ratelimit-*`, sleep until `x-ratelimit-reset` on 429) used by discovery
   *and* `execute_query`, so a 12h reindex never starves user queries.

## `execute_query`

* `str` → DDSQL (default window from config, `row_limit` handling above).
* `dict` → `{"query": "...", "from": "...", "to": "...", "limit": N}` — same, with an explicit window.
* `dict` with `{"kind": "monitors" | "events" | "slos", ...}` → REST passthrough for the
  few objects DDSQL lacks. Events need an explicit `filter[from]` (default
  lookback missed 25-min-old seeded events; `filter[from]=now-1d` found 6).

## Config + credentials

```python
class DatadogConfig(BaseModel):
    site: Literal["datadoghq.com","us3.datadoghq.com","us5.datadoghq.com","datadoghq.eu",
                  "ap1.datadoghq.com","ap2.datadoghq.com","uk1.datadoghq.com","ddog-gov.com"]
    default_window_hours: int = 1          # DDSQL `time` when the query gives none
    only_tables_with_data: bool = True
    max_rows: int = 10_000

class DatadogApiKeyCredentials(BaseModel):      # auth "api_app_keys", scopes system+user
    api_key: SecretStr                           # DD-API-KEY
    application_key: SecretStr                   # DD-APPLICATION-KEY — scope it read-only

class DatadogTokenCredentials(BaseModel):        # auth "access_token" — NOT YET VERIFIED
    access_token: SecretStr                      # Authorization: Bearer (PAT / service-account token)
```

App-key scopes the connector needs (form `description` should list them):
`timeseries_query`, `metrics_read`, `logs_read_data`, `logs_read_index_data`,
`dashboards_read`, `monitors_read`, `events_read` (+ `apm_read`, `usage_read`
if spans/cost matter). The spike ran with an unscoped key — **re-run with a
scoped key** to confirm DDSQL needs nothing beyond these.

`test_connection`: `GET /api/v1/validate` (API key) then `SELECT 1` (app key + DDSQL scope).

## Files (per the skill)

| File | |
|---|---|
| `backend/app/data_sources/clients/datadog_client.py` | client; `requests`, no new dependency |
| `backend/app/data_sources/clients/datadog_tables.json` + generator script | candidate table names |
| `backend/app/schemas/data_sources/configs.py` | config + 2 credential classes |
| `backend/app/schemas/data_source_registry.py` | entry, `category="infra"`, explicit `client_path`, `version="beta"` |
| `frontend/public/data_sources_icons/datadog.svg` (+ mapping if needed) | icon |
| `backend/tests/unit/test_datadog_client.py` | mock HTTP: submit/poll, column-major → DataFrame, ms timestamps, `row_limit+1` truncation, 400 detail passthrough, LIMIT-0 catalog incl. skip-on-400, site→URL, both auth header styles |
| `backend/tests/integrations/ds_clients.py` | `datadog` via `integrations.json` creds (no container exists) |
| `docs/feedback-loops/datadog-connector.md` | seed → spike → live UI loop |

## Open items before/while building

1. **Bearer token auth** against `/api/v2/ddsql/*` — unverified.
2. **Scoped app key** — re-run the spike with only the scopes above.
3. **Logs field discovery** — pick between sampling `dd.logs` and the facets API.
4. **DDSQL rate limit** — 60/20 s, verified. Open: is it per org or per key, and
   is there a max query length for the `UNION ALL` batch (50 tables untested; 28 OK)?
5. **APM data** — `dd.spans` returns empty here; validating it needs a real
   Agent (agentless OTLP span intake is an allowlisted preview).
6. `tools/datadog/*` and seeded data use `env:bow-demo`; rotate the trial keys
   used in the 2026-10-02 spike.
