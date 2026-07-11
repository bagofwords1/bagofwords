# Plan: Splunk connector — `SplunkClient`

## Mission
Add a new data source type `splunk` to bagofwords that lets the agent query a
Splunk Enterprise / Splunk Cloud instance (events across indexes and
sourcetypes, plus `stats`/`timechart` aggregations) through Splunk's REST API
using **SPL** (Search Processing Language). Model it on the existing
**Zabbix** connector, not on the SQL (MSSQL) or BI (PowerBI) connectors —
Splunk, like Zabbix, is an HTTP API with no SQL endpoint, where `execute_query`
runs a query the client submits to the API and `get_schemas` exposes a curated
catalog of virtual tables discovered cheaply from metadata.

This follows the repo's own `.agents/skills/add-connection-type/SKILL.md`. A
connector is registry-driven: the frontend form, auth variants, and client
resolution all derive from one entry in
`backend/app/schemas/data_source_registry.py`.

## Why Zabbix is the template (not MSSQL / PowerBI / OpenSearch)
- **Not SQL, no free schema** — Splunk is schema-on-read. There is no `_mapping`
  to read (that is the OpenSearch/Elastic story), so unlike OpenSearch the
  "columns" are not free. The catalog and its fields are assembled from Splunk
  *metadata* + bounded sampling, exactly the way Zabbix builds a curated catalog
  and best-effort enriches the `items` table.
- **HTTP API, no native driver** — plain HTTPS via `requests` (already a
  dependency). No Dockerfile change, no system library. (`splunklib` is an
  optional convenience for job polling; we stay on raw REST to match Zabbix /
  OpenSearch.)
- **Async query model** — unlike Zabbix's single JSON-RPC POST, a Splunk search
  is an async *job*: POST creates it, then you poll and fetch results. The
  client hides that behind `execute_query`.

## Splunk's data model → the repo's `Table`/`TableColumn`

Splunk has no fixed schema. The closest mapping to a table-with-columns is a
four-level hierarchy; we expose **index + sourcetype as the "table"** (the
option chosen for this connector) and treat fields as lazily-discovered columns:

| Splunk concept | SQL analogy | How the client enumerates it | Cost |
|---|---|---|---|
| **index** | database | REST `GET /services/data/indexes` (config metadata) | ~free, instant |
| **sourcetype** (per index) | **table** | `\| metadata type=sourcetypes index=*` (reads tsidx bucket metadata, not raw events) | cheap, seconds; time-boundable |
| **field** (per sourcetype) | column | bounded sample search: `search index=X sourcetype=Y earliest=-24h \| head 500 \| fieldsummary` | the only real cost — runs over events |
| **data model / CIM** | curated view | `\| tstats … from datamodel=…` | near-free if accelerated; **out of scope v1** |

`get_schemas()` returns one virtual `Table` per `index::sourcetype` pair, named
`"<index>::<sourcetype>"`. Columns (fields) are filled in **lazily and
best-effort**, mirroring the Zabbix `items` enrichment: never fail discovery on
a sampling error, cache what you find, and bound every sample by `| head` + an
`earliest` window. Tables whose fields have not yet been sampled carry a
description noting Splunk is schema-on-read and fields are discovered on query.

### The cost model (the design's central concern)
- **Ingest/license is *not* consumed by search** — discovery never burns license
  quota. The cost is search-head/indexer CPU + concurrent-search slots + latency.
- Discovery cheapness ladder, and the mitigations baked into the client:
  1. **indexes** — free (REST config endpoint).
  2. **sourcetypes** — cheap (`| metadata`, tsidx metadata, `earliest=-Nd`).
  3. **fields** — bounded cost: sample one sourcetype at a time, `| head 500`,
     narrow window, **cache the result**, and make it lazy so `get_schemas()`
     stays fast. Deep field discovery only runs on demand / first query.
- A `discovery_window_days` config bounds the metadata + sampling windows so a
  connect + schema pass can never launch an all-time scan.

## Query spec (what `execute_query` accepts)
An **SPL string** (the natural Splunk interface), or a JSON envelope wrapping
one so the client can inject guards:

```json
{"spl": "search index=web sourcetype=access_combined status>=500 | stats count by host",
 "earliest": "-24h", "latest": "now", "limit": 1000}
```

- `spl` (required): the search. A leading `search`/`|` is respected; the client
  prepends `search ` when the string starts with a bare field expression, and
  refuses obviously unbounded generating searches without a time window.
- `earliest`/`latest` (optional): time bounds; default from
  `discovery_window_days`.
- `limit` (optional): row cap, applied as a trailing `| head N` and enforced by
  `MAX_ROWS`.

Execution flow (hidden inside `execute_query`): `POST /services/search/v2/jobs`
with `output_mode=json`, `exec_mode=blocking` (or create + poll `.../<sid>`
until `isDone`), then `GET .../<sid>/results?output_mode=json&count=<limit>` →
normalize the result rows into a DataFrame.

`system_prompt()` documents SPL basics for the agent plus Splunk gotchas:
`_time` is the event timestamp (epoch), the difference between transforming
commands (`stats`, `timechart`, `top`) and raw event search, that fields are
schema-on-read (so unknown fields simply come back empty, they are not errors),
and to always bound searches with `earliest`.

## Authentication
Two variants (same shape as Zabbix), each `scopes=["system", "user"]`:

- **`token` (default)** — a Splunk **authentication token** (Settings → Tokens,
  or `splunk create authtoken`) sent as `Authorization: Bearer <token>`.
  Cleanest; works on Splunk Cloud and 8.x+; the per-user scope means
  bring-your-own-token.
- **`userpass`** — Splunk username + password over HTTP basic against the
  management port. Works on older on-prem installs. (Splunk sessions could be
  minted via `/services/auth/login`, but basic-auth-per-request is simpler and
  avoids session lifecycle; use it unless a reason emerges.)

`test_connection()` calls `GET /services/server/info` (cheap, authed) to confirm
the endpoint + credentials and report the Splunk version.

## Files to create / modify (in order, per the skill)

1. **Create** `backend/app/data_sources/clients/splunk_client.py`
   - `class SplunkClient(DataSourceClient)`, `capabilities = {Capability.QUERY}`.
   - `__init__(host, port=8089, api_token=None, username=None, password=None,
     verify_ssl=True, discovery_window_days=7, index_pattern=None)`.
   - `connect()` — `requests.Session`; helper `_request()` that hits the Splunk
     management REST endpoint and raises readable errors on 401/403/404/5xx;
     `_run_search(spl, earliest, latest, limit)` that owns the job lifecycle.
   - `get_schemas()` / `get_schema()` — indexes (`GET /services/data/indexes`)
     × sourcetypes (`| metadata`) → virtual tables; lazy/cached/bounded
     `_sample_fields()` for columns; `index_pattern` filters which indexes are
     exposed.
   - `test_connection()`, `execute_query()`, `prompt_schema()` (via
     `ServiceFormatter`), `system_prompt()`, `description`.
   - Keep calls sync — the base provides the async wrappers.
2. **Edit** `backend/app/schemas/data_sources/configs.py`
   - `SplunkConfig` — `host` (required; bare host or full URL), `port: int = 8089`,
     `verify_ssl: bool = True`, `discovery_window_days: int = 7`,
     `index_pattern: Optional[str] = None` (comma-separated indexes/globs).
   - `SplunkTokenCredentials` — `api_token` (`ui:type: password`).
   - `SplunkUserPassCredentials` — `username`, `password` (`ui:type: password`).
3. **Edit** `backend/app/schemas/data_source_registry.py`
   - Import the three schemas.
   - Add a `"splunk"` entry to `REGISTRY`: `config_schema=SplunkConfig`,
     `credentials_auth=AuthOptions(default="token", by_auth={"token": …,
     "userpass": …})`, **explicit**
     `client_path="app.data_sources.clients.splunk_client.SplunkClient"`,
     `requires_license="enterprise"`, `version="beta"`.
   - Add `"splunk"` to `ENTERPRISE_DATASOURCES` in `backend/app/ee/license.py`.
4. **Icon** — add `frontend/public/data_sources_icons/splunk.png`.
   `DataSourceIcon.vue` auto-resolves `/data_sources_icons/<type>.png`, so no
   frontend code change is expected.
5. **Driver** — none. `requests` is already present; no `pyproject.toml` /
   `Dockerfile` change.

## Verification (sandbox-feedback-loop)

Two layers — a reliable always-green mock unit test, plus a container-based
integration test so CI can exercise a real instance without stored secrets.

### 1. Registry + import resolves
```bash
cd backend
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('splunk'))"
```

### 2. Unit test (mock the HTTP boundary — always green, no network)
`backend/tests/unit/test_splunk_client.py`, in the style of
`tests/unit/test_zabbix_client.py`: mock `requests` / `_request` and assert
- `get_schemas()` returns the expected `index::sourcetype` virtual tables and
  survives a field-sampling failure (enrichment is best-effort);
- `execute_query()` submits the right SPL job, applies the `head`/time guards,
  and normalizes JSON results into a DataFrame;
- Bearer-header (token) vs basic-auth (userpass) paths;
- `test_connection()` reports success/failure and surfaces REST errors.

### 3. Generic data-source e2e still green
```bash
cd backend
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
```

### 4. Integration test against a real Splunk (testcontainers)
Add `"splunk"` to `DATA_SOURCES` in `backend/tests/integrations/ds_clients.py`
and a single-container `CONTAINER_REGISTRY` entry (much lighter than Zabbix's
compose stack — Splunk is one image). Seed data via the tooling below.

### 5. The sandbox — `tools/splunk/`
Reproducible local environment mirroring `tools/zabbix/`:
- `docker-compose.yaml` — one `splunk/splunk:latest` service,
  `SPLUNK_START_ARGS=--accept-license`, `SPLUNK_PASSWORD` from env, ports
  `8000` (web), `8088` (HEC), `8089` (management/REST). The free trial license
  (500 MB/day) is ample for a seed.
- `seed_splunk.py` — enables **HEC** and bulk-POSTs hundreds of thousands of
  synthetic events across a few indexes/sourcetypes (e.g. `web/access_combined`,
  `app/log4j`, `metrics/collectd`) spanning the last 7 days, so field discovery
  and `stats`/`timechart` queries have real data. Prints an auth token on the
  last line as `SPLUNK_TOKEN=<token>` (Zabbix-seed parity).
- Optionally `build_dashboard.py` for a saved search / panel, matching the
  Zabbix tooling.

### 6. Live UI pass
`tools/agent/boot_stack.sh` + `seed_org.py`, then in the app: create a Splunk
connection → **Test connection** succeeds → the tables selector lists the
`index::sourcetype` tables → run a prompt that queries events and an
aggregation. Capture the connect form + tables list (**ui-evidence** skill).

### 7. Record the loop
Add `docs/feedback-loops/splunk-connector.md` (**sandbox-feedback-loop** skill),
mirroring `docs/feedback-loops/zabbix-connector.md`.

## Pitfalls
- **Always set `client_path`.** `splunk` → naive title-casing gives
  `SplunkClient`, which happens to match — but the explicit path is the
  contract (see the ServiceNow root-cause in the Zabbix feedback loop).
- **Schema discovery must stay cheap.** `get_schemas()` uses `| metadata`, not
  `fieldsummary` sweeps; field sampling is lazy, per-sourcetype, `| head`-capped,
  time-bounded, cached, and **never fails discovery** — exactly the Zabbix
  `items`-enrichment contract.
- **Every search needs a time bound.** Default `earliest` from
  `discovery_window_days` and refuse unbounded generating searches, or a single
  agent query can pin a production search head.
- **Async job lifecycle** — a Splunk search is create → poll → fetch, not one
  call. Cap wall-clock and row count; surface job errors/messages readably.
- **Ingest ≠ search cost** — call this out in `system_prompt()` so the agent
  understands searching is safe on license but should stay bounded.
- **`is_connection`** — leave it unset (True); it's a real data source, not an
  MCP-style tool provider.
- Registry `description` and config field descriptions are product copy — write
  them for end users, not as code comments.

## Scope summary
~2 files new (client, unit test) + config/registry/icon/license edits and a
single-image compose sandbox. No SQL, no native driver, no Dockerfile change,
no frontend-form work.
