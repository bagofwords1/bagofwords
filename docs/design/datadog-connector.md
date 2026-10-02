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

## Catalog (`get_schemas`) — the spike's main answer

**DDSQL cannot list its own tables.** `information_schema.tables`,
`information_schema.columns`, `pg_catalog.pg_tables`, `SHOW TABLES` and
`dd.schemas` all fail (400). But:

* **`SELECT * FROM <table> LIMIT 0` returns the full typed column list with zero
  rows** (`dd.hosts` → 16 cols, `aws.ec2_instance`, `k8s.pods` all resolve even
  with no data), and
* an unknown table is a clean 400 `non-existent dataset`.

So discovery = **known table names × `LIMIT 0` probe**:

1. **Candidate names**: a static list shipped with the client
   (`backend/app/data_sources/clients/datadog_tables.json`), generated from the
   public [DDSQL Data Directory](https://docs.datadoghq.com/ddsql_reference/data_directory/)
   (`aws.*`, `azure.*`, `gcp.*`, `oci.*`, `k8s.*`, `dd.*` — hundreds of tables).
   A small script regenerates it; stale entries just fail the probe and are skipped.
2. **Columns**: `LIMIT 0` per candidate, run with bounded concurrency and a
   `progress_callback`. Cache per connection (inventory schemas are stable).
3. **Keep only tables with data** (config toggle, default on): `SELECT 1 FROM t LIMIT 1`
   — a trial org has `dd.hosts` but no `aws.*`; listing hundreds of empty AWS
   tables would drown the planner.
4. **Function tables** (always present, hand-written entries): `dd.logs`,
   `dd.spans`, `dd.metrics_timeseries`, `dd.metrics_scalar`, `dd.cloud_cost_*`.
   Their "columns" are the useful fields:
   * `dd.logs` — reserved fields + top facets, sampled once via
     `dd.logs(columns => ARRAY['*'])`-style probe or the log facets list (to verify);
   * `dd.metrics_*` — description embeds the org's metric names from
     `GET /api/v2/metrics` (verified 200; paged), capped, no table per metric.
5. **Knowledge tables** (Splunk-dashboard pattern):
   * `dashboard::<title>` — one column per widget, carrying its query
     (`GET /api/v1/dashboard`, `/dashboard/{id}`; verified 200).
   * `monitors` — one table, rows = monitors with query + thresholds
     (`GET /api/v1/monitor`; verified 200, 7 monitors in the trial org).
     `dd.monitors` is **not** a DDSQL dataset (verified 400), so this goes over REST
     and `execute_query` needs a tiny JSON-spec path for it — see below.

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
4. **DDSQL rate limits** — not hit in the spike; read `X-RateLimit-*` headers
   during the hundreds-of-probes catalog pass and size concurrency from them.
5. **APM data** — `dd.spans` returns empty here; validating it needs a real
   Agent (agentless OTLP span intake is an allowlisted preview).
6. `tools/datadog/*` and seeded data use `env:bow-demo`; rotate the trial keys
   used in the 2026-10-02 spike.
