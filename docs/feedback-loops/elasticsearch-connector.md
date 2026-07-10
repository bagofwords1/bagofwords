# Feedback Loop — Elasticsearch connector (new)

Validates the new Elasticsearch data source connector end-to-end: schema-driven
admin form → connection → live schema discovery (indices/patterns/data streams
from `_mapping`) → agent table selection → LLM prompts building log-investigation
charts from real Elasticsearch data. Design doc: `docs/design/elastic-connector.md`.

Elasticsearch is modeled on the in-repo **OpenSearch** connector (OpenSearch is
a fork of Elasticsearch 7.10): indices map to catalog tables, the index
*mapping* is the schema (so discovery is a single bulk `GET /_mapping` — no
document sampling, no cost), and queries are the native DSL in a JSON envelope
with SQL/ES|QL escape hatches. It is a **community** data source (NOT
enterprise-gated), `data_shape="objects"`.

## What was added

- `backend/app/data_sources/clients/elasticsearch_client.py` — REST client
  forked from `opensearch_client.py`. Reuses the mapping-flatten and
  aggregation-flatten logic; adds: ApiKey/basic/none auth, ES `_sql`
  (`POST /_sql?format=json`) and ES|QL (`POST /_query`) escape hatches, and
  **date-suffix pattern collapsing** — rolling daily indices
  (`security-2026.07.10`, `…07.09`, …) collapse into one `security-*` table
  (union of fields) so the catalog stays a handful of *patterns*, not one
  table per day. This is what lets the agent search `security-*,frontend-*`
  the way an analyst does.
- `ElasticsearchConfig` / `ElasticsearchApiKeyCredentials` /
  `ElasticsearchCredentials` / `ElasticsearchNoAuthCredentials` in
  `backend/app/schemas/data_sources/configs.py`; registry entry (type
  `elasticsearch`, explicit `client_path`, default auth `apikey`, **no
  `requires_license`**) in `backend/app/schemas/data_source_registry.py`.
- `frontend/public/data_sources_icons/elasticsearch.png`.
- `backend/tests/unit/test_elasticsearch_client.py` (19 tests).
- `tools/elastic/` — reproducible local environment: `docker-compose.yaml`
  (Elasticsearch 8.15, security on / HTTP-TLS off), `seed_elastic.py`
  (13k log events across 10 service patterns × 5 days, prints an API key).

## Loop A — deterministic reproduction (no external services)

Mock the HTTP boundary; runs in a clean sandbox with no Elasticsearch server.

```bash
cd backend
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('elasticsearch'))"
# -> <class 'app.data_sources.clients.elasticsearch_client.ElasticsearchClient'>
uv run pytest tests/unit/test_elasticsearch_client.py -q
# -> 19 passed
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
# -> 11 passed
```

The unit tests assert the invariants, not one scenario: the three auth headers,
date-suffixed indices collapsing to one `<base>-*` union table (single-day
indices left concrete, `.`-system indices excluded), multi-field `.keyword`
surfacing, the `size+from` window guard, aggregation flattening, and SQL/ES|QL
dispatch + response shapes.

## Loop B — live confirmation (real Elasticsearch 8.15.3)

```bash
cd tools/elastic
docker compose up -d                    # ES 8.15 on :9200 (http, security on)
python3 seed_elastic.py                 # 13k log events; prints ES_API_KEY=<id:key>
```

Then, driving the real `ElasticsearchClient` (API key from the seed):

```
test_connection            -> {'success': True, 'message': 'Connected to Elasticsearch 8.15.3'}
get_schemas()              -> 10 tables, all kind=pattern:
                              backend-*, billing-*, default-*, delivery-*, edge-*,
                              frontend-*, network-*, security-*, storage-*, system-*
                              (365 daily indices would have collapsed the same way)
each table                 -> 13 columns straight from _mapping (@timestamp, service,
                              level, status, syslog_severity, message + message.keyword, …)
multi-pattern log query    -> index="security-*,frontend-*",
  query_string level:(error OR fatal) OR status:>=500, aggs by service + by level
                           -> frontend 760, security 732 / error 587, fatal 298, warn 206
```

### Full-app agentic pass (the running product)

With the stack booted (`tools/agent/boot_stack.sh`), an Anthropic LLM provider
configured, and the connection created through the catalog:

- Admin catalog shows **Elasticsearch** (icon, community — no license badge) →
  connect form (schema-generated from `ElasticsearchConfig`) → **Test
  connection: "Connected to Elasticsearch 8.15.3"** → tables selector lists all
  10 `<service>-*` pattern tables (10/10 active).
- Backend log confirms the agent constructs `ElasticsearchClient` and queries
  it: `construct_client: Resolved ClientClass=ElasticsearchClient`, then
  `create_data.schema_build … resolve_active patterns=10`,
  `create_data.viz_infer got_raw=True`.
- Prompt *"count error and fatal events by service across security-* and
  frontend-*, last 7 days, bar chart"* → the agent runs a `query_string`
  aggregation across both patterns and renders a bar chart. Screenshots under
  `media/es-splunk/`.

## What this proves / regression notes

- The connector resolves via the registry, is community-gated, discovers its
  catalog from `_mapping` for free (one call), collapses rolling indices into
  patterns, and executes real DSL/aggregation queries against Elasticsearch 8.
- The unit suite survives as a regression test (mocked boundary, no network).
- Pre-existing unrelated issue observed while driving the app: the LLM model
  create/PATCH route raises `AttributeError: 'LLMService' object has no
  attribute 'create_model'` — independent of this change (register the default
  model via the DB instead). Harmless OpenTelemetry "Failed to detach context"
  log lines are also pre-existing and unrelated.
