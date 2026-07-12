# Plan: Elasticsearch connector — `ElasticsearchClient`

## Mission
Add a new data source type `elasticsearch` to bagofwords that lets the agent
query an Elasticsearch cluster (indices + data streams) through the REST API
using the query DSL, aggregations, ES|QL, or SQL. Model it directly on the
existing **`opensearch_client.py`** — OpenSearch is a fork of Elasticsearch
7.10, so the shape is already in-repo: indices map to catalog tables, the index
*mapping* is the schema, and queries are the native DSL in a JSON envelope with
an SQL escape hatch.

This follows `.agents/skills/add-connection-type/SKILL.md`. The connector is
registry-driven; the frontend form derives from the config/credentials schemas.

## Why OpenSearch is the template (and why Elastic is the *easy* connector)
- **Schema is free.** Unlike Splunk (schema-on-read, sampling required),
  Elasticsearch has a real schema: `GET /<index>/_mapping` returns field → type
  for every field. `get_schemas()` is a single bulk `GET /_mapping` call — no
  document sampling, no cost concern. This is the crux of the "how do I get
  schemas" question: **for Elastic it is one cheap REST call.**
- **Same query surface.** DSL search + aggregations map 1:1 to the existing
  OpenSearch client. The differences from OpenSearch are small and localized
  (auth options, SQL/ES|QL endpoint paths, ES 8 security defaults).

The connector is therefore largely a **fork + rename** of the OpenSearch client
with a handful of Elastic-specific edits, not a from-scratch build.

## Data model → the repo's `Table`/`TableColumn`

| Elastic concept | SQL analogy | How the client enumerates it | Cost |
|---|---|---|---|
| **index / data stream** | table | `GET /_mapping` (bulk) + `GET /_data_stream` | ~free, one call |
| **field** (from mapping) | column | flattened from the mapping `properties` | free (in the same call) |
| **index alias / pattern** | view | `index_pattern` config restricts exposure | free |

`get_schemas()` returns one `Table` per non-system index (skip `.`-prefixed),
with columns flattened from the mapping properties — exactly
`opensearch_client.py`'s `get_tables()` / `_table_from_mapping()` /
`_flatten_properties()`. Data streams resolve to their backing indices'
mappings, same as OpenSearch.

## Query spec (what `execute_query` accepts)
The same JSON envelope as OpenSearch (keep the contract identical so the agent's
mental model transfers):

```json
{"index": "orders",
 "query": {"range": {"@timestamp": {"gte": "now-24h"}}},
 "aggs": {"by_status": {"terms": {"field": "status"}}},
 "size": 100, "sort": [{"@timestamp": "desc"}], "_source": ["status", "amount"]}
```

- `index` (required), `query`, `aggs`, `size`, `sort`, `_source`, `from` →
  `POST /{index}/_search`; `size + from` capped at `max_result_window` (10 000).
- **SQL escape hatch** — Elastic's endpoint differs from OpenSearch's
  `/_plugins/_sql`: use `POST /_sql?format=json` with `{"query": "SELECT …"}`.
- **ES|QL (optional, ES 8.11+)** — a `esql` key posting to `POST /_query`
  (`{"query": "FROM orders | STATS count() BY status"}`). Newer, piped, SQL-ish;
  nice-to-have, gate behind a version check.

`system_prompt()` documents the envelope + Elastic specifics: `@timestamp` is
the conventional time field, aggregation results are flattened, `keyword`
vs `text` field types for exact-match vs analyzed search, and the SQL/ES|QL
alternatives.

## Authentication
Three variants, `scopes=["system", "user"]`:

- **`apikey` (default)** — Elastic **API key** sent as
  `Authorization: ApiKey <base64(id:key)>`. The recommended Elastic 8.x path;
  per-user scope = bring-your-own key.
- **`userpass`** — HTTP basic (`elastic` superuser or a role user). Mirrors the
  OpenSearch `userpass` variant.
- **`none`** — security disabled / network-gated dev clusters (the OpenSearch
  `OpenSearchNoAuthCredentials` analogue).

`test_connection()` calls `GET /` (cluster info) to confirm endpoint +
credentials and report the ES version.

## Files to create / modify (in order)

1. **Create** `backend/app/data_sources/clients/elasticsearch_client.py`
   - `class ElasticsearchClient(DataSourceClient)`. Fork `OpenSearchClient`:
     reuse `get_tables`/`get_schemas`/`get_schema`, `_table_from_mapping`,
     `_flatten_properties`, `_flatten_aggregations`, the search envelope.
   - Change: `_auth_header()` supports ApiKey/basic/none; `_execute_sql()`
     targets `POST /_sql?format=json`; add optional `_execute_esql()` →
     `POST /_query`; `test_connection()` hits `GET /`.
   - `__init__(host, port=9200, api_key=None, user=None, password=None,
     secure=True, verify_certs=True, index_pattern=None)`.
2. **Edit** `backend/app/schemas/data_sources/configs.py`
   - `ElasticsearchConfig` — same fields as `OpenSearchConfig` (`host`, `port`
     default 9200, `secure`, `verify_certs`, `index_pattern`), `secure`
     defaulting to True (ES 8 is TLS-by-default).
   - `ElasticsearchApiKeyCredentials` (`api_key`, password field),
     `ElasticsearchCredentials` (`user`/`password`),
     `ElasticsearchNoAuthCredentials` (empty).
3. **Edit** `backend/app/schemas/data_source_registry.py`
   - Add an `"elasticsearch"` entry mirroring `"opensearch"`:
     `config_schema=ElasticsearchConfig`,
     `credentials_auth=AuthOptions(default="apikey", by_auth={"apikey": …,
     "userpass": …, "none": …})`, **explicit**
     `client_path="app.data_sources.clients.elasticsearch_client.ElasticsearchClient"`,
     `requires_license="enterprise"`.
   - Add `"elasticsearch"` to `ENTERPRISE_DATASOURCES` in
     `backend/app/ee/license.py` (matching how `opensearch`/`zabbix` are gated).
4. **Icon** — add `frontend/public/data_sources_icons/elasticsearch.png`.
5. **Driver** — none. Raw `requests`, same as OpenSearch (no `elasticsearch-py`
   dependency, avoiding its strict server-version handshake).

## Verification (sandbox-feedback-loop) — same shape as Splunk

### 1. Registry + import resolves
```bash
cd backend
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('elasticsearch'))"
```

### 2. Unit test (mock the HTTP boundary — always green)
`backend/tests/unit/test_elasticsearch_client.py`, mirroring the OpenSearch unit
tests: assert `get_schemas()` flattens mappings into the expected columns,
`execute_query()` builds the right `_search` body and flattens aggregations,
the `size+from` window guard, the three auth headers, and SQL/ES|QL dispatch.

### 3. Generic data-source e2e still green
```bash
cd backend
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
```

### 4. Integration test against a real Elasticsearch (testcontainers)
Add `"elasticsearch"` to `DATA_SOURCES` in
`backend/tests/integrations/ds_clients.py` and a single-image
`CONTAINER_REGISTRY` entry (`docker.elastic.co/elasticsearch/elasticsearch:8.x`,
`discovery.type=single-node`, security tuned for the test).

### 5. The sandbox — `tools/elastic/`
- `docker-compose.yaml` — one `elasticsearch:8.x` service,
  `discovery.type=single-node`, `xpack.security.enabled` set for the chosen auth
  demo, port `9200`.
- `seed_elastic.py` — bulk-index a few indices with real mappings and a large
  document count (e.g. `orders`, `logs-app`, `metrics-host`) so aggregations and
  DSL queries have data. Prints an API key / creds on the last line, matching
  the Zabbix/Splunk seed convention.

### 6. Live UI pass
Create an Elasticsearch connection → **Test connection** succeeds → tables
selector lists the indices → prompt an aggregation and a DSL search. Capture the
form + tables list (**ui-evidence** skill).

### 7. Record the loop
`docs/feedback-loops/elasticsearch-connector.md`.

## Pitfalls
- **Reuse, don't reinvent** — the mapping-flattening and aggregation-flattening
  logic already exists and is tested in `opensearch_client.py`; diverging risks
  subtle DataFrame-shape drift between the two connectors.
- **ES 8 is TLS + security by default** — the container needs security config
  for a clean demo; default `secure=True` and document `verify_certs=false` for
  the self-signed demo cert (same note OpenSearch carries).
- **SQL/ES|QL endpoints differ from OpenSearch** — `/_sql` and `/_query`, not
  `/_plugins/_sql`; version-gate ES|QL (8.11+).
- **Always set `client_path`** — `elasticsearch` → `ElasticsearchClient` matches
  title-casing, but the explicit path is the contract.
- **`is_connection`** — leave unset (True).
- Registry/config descriptions are user-facing product copy.

## Scope summary
Largely a fork of `opensearch_client.py`: 1 new client + unit test, config/
registry/icon/license edits, and a single-image compose sandbox. No SQL engine,
no native driver, no Dockerfile change, no frontend-form work.

## Relationship to the Splunk connector
Both connectors share the **same verification harness** (single-container
docker-compose under `tools/<type>/`, a seed script printing an auth token on
its last line, mocked-boundary unit tests as Loop A, seeded-container as Loop B,
recorded in `docs/feedback-loops/<type>-connector.md`). The **only** conceptual
difference is schema discovery: Splunk must sample fields (schema-on-read, the
cost lives here), while Elasticsearch reads its schema for free from `_mapping`.
