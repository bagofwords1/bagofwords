# Feedback Loop — OpenSearch connector

Adds an **OpenSearch** data source connector: a `DataSourceClient` subclass
speaking plain REST (no engine SDK), Pydantic config/credentials schemas, and
a `REGISTRY` entry (`dev_only=True`, beta). Design doc:
`docs/design/opensearch-connector.md`. This is the runnable loop used to build
and validate it in a fresh cloud sandbox — Loop A is fully mocked, Loop B runs
a real populated OpenSearch 2.19.1 in the sandbox.

## What was added

| Layer | File | Change |
|-------|------|--------|
| Client | `backend/app/data_sources/clients/opensearch_client.py` | New `OpenSearchClient(DataSourceClient)` (+ `OpensearchClient` alias for dynamic naming) |
| Config | `backend/app/schemas/data_sources/configs.py` | `OpenSearchConfig`, `OpenSearchCredentials`, `OpenSearchNoAuthCredentials` |
| Registry | `backend/app/schemas/data_source_registry.py` | `"opensearch"` entry: explicit `client_path`, `data_shape="objects"`, `dev_only=True`, `version="beta"` |
| Driver | — | none: plain `requests` (already a dependency), no Dockerfile change |
| Icon | `frontend/public/data_sources_icons/opensearch.png` | Official logo (darkmode variant, per request) |
| Unit tests | `backend/tests/unit/test_opensearch_client.py` | 37 tests, transport mocked |
| Integration | `backend/tests/integrations/ds_clients.py` | `opensearch` in `DATA_SOURCES`; `_OpenSearchContainer` + new generic `seed_fn` hook in `CONTAINER_REGISTRY` (the `seed_sql` path is SQLAlchemy-only) |

Key design points (rationale in the design doc):

- **Schema = the index mapping.** `get_schemas()` is one bulk `GET /_mapping`
  (+ `GET /_alias`): no document sampling. Fields flatten to dot-path columns
  using the MongoDB client's conventions — `object` recurses
  (`customer.tier`), `nested` gets an `array` column plus `items[].sku`
  children, multi-fields surface (`title.keyword`) so the coder can pick the
  aggregatable variant. `.`-prefixed system indices are excluded; aliases
  surface as union tables.
- **Queries** are a JSON envelope over `POST /{index}/_search` (query DSL +
  aggregations, whitelisted keys, `size` defaults 100 / 0-with-aggs, result
  window capped at 10k), with a `{"sql": ...}` escape hatch to the bundled
  SQL plugin. Agg results flatten recursively: bucket levels become key
  columns, metric leaves become value columns.
- **Auth**: HTTP basic (security plugin default) or none (system scope only);
  `verify_certs` toggle for self-signed demo certs; `host` accepts a full URL.

## Environment setup (fresh sandbox)

```bash
cd backend
pip install uv
uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db"
mkdir -p db
```

## Loop A — unit validation (no live OpenSearch)

Self-contained: `requests.request` is monkeypatched at the module boundary
with a `{(method, path): response}` routing table. Covers URL/auth/TLS
construction, mapping flattening (object / nested / multi-field), system-index
exclusion, alias union, envelope validation, hits→DataFrame, size defaults and
the 10k window cap, aggregation flattening (terms+metrics, nested levels,
date_histogram `key_as_string`, `filters` dict buckets, stats, percentiles,
unknown-shape fallback), SQL escape hatch, `test_connection`, and registry
wiring.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/unit/test_opensearch_client.py -q
```

**Observed (PASS):** `37 passed, 207 warnings in 73.10s`

Registry + e2e regression legs:

```bash
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('opensearch'))"
# <class 'app.data_sources.clients.opensearch_client.OpenSearchClient'>
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
# 11 passed, 1158 warnings in 41.99s
```

## Loop B — live confirmation (real OpenSearch in the sandbox) — RUN

The sandbox has Docker but **dockerd is not started**; boot it with the
pre-configured proxy env, then run a single node with security disabled:

```bash
HTTPS_PROXY=$DOCKER_HTTPS_PROXY HTTP_PROXY=$DOCKER_HTTPS_PROXY setsid dockerd \
  > /tmp/dockerd.log 2>&1 &
until docker info >/dev/null 2>&1; do sleep 1; done

docker run -d --name os-dev -p 9200:9200 \
  -e discovery.type=single-node \
  -e DISABLE_SECURITY_PLUGIN=true \
  -e "OPENSEARCH_JAVA_OPTS=-Xms512m -Xmx512m" \
  opensearchproject/opensearch:2.19.1
```

(~1.0 GiB RSS; ready in ~15s.) Seed a populated `orders` index exercising
every mapping feature the flattener handles — object field
(`customer.{name,tier}`), nested field (`items.{sku,qty}`), multi-field
(`title` + `title.keyword`), an alias (`recent_orders`), 4 docs via
`_bulk?refresh=true` — then run the real client:

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run python - <<'PY'
from app.data_sources.clients.opensearch_client import OpenSearchClient
c = OpenSearchClient(host="localhost", port=9200)
print(c.test_connection())
for t in c.get_schemas():
    print(t.name, t.metadata_json["type"], [f"{x.name}:{x.dtype}" for x in t.columns])
print(c.execute_query('{"index":"orders","query":{"bool":{"filter":[{"term":{"status":"active"}}]}},"sort":[{"created_at":"desc"}],"_source":["order_id","total","customer.tier"]}'))
print(c.execute_query('{"index":"orders","aggs":{"by_status":{"terms":{"field":"status"},"aggs":{"by_tier":{"terms":{"field":"customer.tier"},"aggs":{"revenue":{"sum":{"field":"total"}}}}}}}}'))
print(c.execute_query('{"index":"orders","query":{"nested":{"path":"items","query":{"term":{"items.sku":"A1"}}}},"_source":["order_id"]}'))
print(c.execute_query('{"sql":"SELECT status, COUNT(*) AS n, SUM(total) AS revenue FROM orders GROUP BY status"}'))
PY
```

**Observed (PASS), abridged:**

```
{'success': True, 'message': 'Connected to opensearch 2.19.1'}
orders index ['created_at:datetime', 'customer.name:string', 'customer.tier:string',
              'items:array', 'items[].qty:integer', 'items[].sku:string',
              'order_id:string', 'status:string', 'title:string',
              'title.keyword:string', 'total:number']
recent_orders alias [same 11 columns]

# hits query -> _id + dot-path columns, sorted desc
   _id  total order_id customer.tier
0  ...   42.0       o4        bronze
1  ...   80.0       o2        silver
2  ...  120.5       o1          gold

# two-level terms agg + sum -> flat rows
   by_status by_tier  doc_count  revenue
0     active  bronze          1     42.0
1     active    gold          1    120.5
2     active  silver          1     80.0
3  cancelled    gold          1     15.0

# nested query matched o1, o2; SQL escape hatch:
      status  n  revenue
0     active  3    242.5
1  cancelled  1     15.0
```

### Container-mode integration test (same leg, via testcontainers)

`tests/integrations/integrations.json` (local only, gitignored — CI restores
it from `INTEGRATIONS_JSON_B64`; the CI copy needs
`"opensearch": {"enabled": true, "container": true}` added for this leg to
run there):

```json
{"data_sources": {"opensearch": {"enabled": true, "container": true}}}
```

```bash
uv run pytest tests/integrations/ds_clients.py -k opensearch -v
```

**Observed (PASS):** `1 passed, 8 deselected in 29.36s` — container starts,
`seed_fn` bulk-indexes, `test_connection` + `get_schemas` succeed through the
dynamic-naming path (`OpensearchClient` alias).

## Live UI pass

`tools/agent/boot_stack.sh` + `uv run python ../tools/agent/seed_org.py`, then
in the app: OpenSearch appears in the add-connection grid (`dev_only` — the
stack runs with `TESTING=true`, a dev environment) → create the connection
against `localhost:9200` (auth: No Authentication) → **Test connection**
succeeds → Tables Selector lists `orders` + `recent_orders` with their
fields. Screenshots: `media/pr/opensearch-connector/` (connect form, tables
list) — the form is schema-generated, so this doubles as review of the config
schemas.

## What this proves / regression notes

- The registry resolves `"opensearch"` via the explicit `client_path`, the
  config/credentials schemas validate and render the connect form, and both
  client-naming conventions work.
- Schema discovery and the query surface (DSL hits, multi-level agg
  flattening, nested queries, aliases, SQL plugin) are correct against a
  **real** OpenSearch 2.19.1, not just the mocked contract.
- The client degrades gracefully: discovery errors → `[]`, connection
  failures → `{"success": False, ...}`, HTTP errors raise with status + body.
- Pre-existing warnings (pydantic `schema` shadowing, `utcnow` deprecations)
  reproduce without this change and are unrelated.
