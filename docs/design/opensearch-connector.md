# OpenSearch / Elasticsearch connector — design & plan

Plan for adding **OpenSearch** and **Elasticsearch** data source connectors,
following the same registry-driven pattern as PostgreSQL / MongoDB: a
`DataSourceClient` subclass, Pydantic config + credentials schemas, and
`REGISTRY` entries. No implementation yet — this doc records the design
decisions, the file-by-file plan, and the **validated** sandbox feedback loop
(OpenSearch 2.19.1 was stood up and probed live in a fresh cloud sandbox on
2026-07-07; observed outputs below).

---

## Scope & recommendation

Build **both** engines from **one shared implementation**, shipped in two
phases:

1. **Phase 1 — OpenSearch** (`type="opensearch"`, `dev_only=True` while
   incubating). OpenSearch is fully verifiable in the sandbox (Apache-2.0
   image, no license gate) — the whole loop runs locally.
2. **Phase 2 — Elasticsearch** (`type="elasticsearch"`) as a thin subclass on
   the same base, differing only in auth variants and endpoint paths.
   Elasticsearch's default image runs with security enabled and the free Basic
   license; `docker.elastic.co/elasticsearch/elasticsearch:8.x` with
   `xpack.security.enabled=false` gives the same local loop.

The two engines forked at Elasticsearch 7.10 and remain API-compatible for
everything this connector needs (`_cat/indices`, `_mapping`, `_search`,
`_aliases`, SQL). The divergences are small and isolatable:

| Concern | OpenSearch | Elasticsearch |
|---|---|---|
| SQL endpoint | `POST /_plugins/_sql` (bundled) | `POST /_sql?format=json` (Basic license) |
| API-key auth | not native (security plugin: basic/JWT) | `Authorization: ApiKey <base64>` |
| Product check | none | ES 8+ clients check `X-Elastic-Product` header |
| Default TLS | security plugin ships self-signed demo certs | security on by default, self-signed in dev |

Follow the `graph_drive_client.py` precedent: one module hosting a shared base
class plus two concrete classes (`OpenSearchClient`, `ElasticsearchClient`),
each referenced by an explicit `client_path`.

---

## Design decisions

### Transport: plain REST via `requests` — no engine SDK

The connector needs ~6 REST endpoints, all simple JSON over HTTP. The official
SDKs actively fight a dual-engine connector: `elasticsearch-py` ≥8 refuses to
talk to non-Elastic servers (product check), and `opensearch-py` is not
supported against ES 8+. `requests` is already a backend dependency (used by
the Pinot/Sisense/Oracle BI clients), so **no new driver dependency and no
Dockerfile change**. If we later want SigV4 auth for Amazon OpenSearch
Service, add `opensearch-py` lazily at that point (see Open questions).

Endpoints used:

- `GET /` — version/distribution probe (also powers `test_connection`)
- `GET /_cat/indices?format=json&h=index,docs.count,status` — index catalog
- `GET /_alias` — alias catalog (aliases surface as queryable "tables")
- `GET /{index}/_mapping` — field catalog (one bulk `GET /*/_mapping` call)
- `POST /{index}/_search` — query DSL execution
- `POST /_plugins/_sql` / `POST /_sql?format=json` — SQL escape hatch

### Shape: document-based, like MongoDB

Registry axes: `data_shape="objects"`, `is_document_based=True`,
`catalog_ownership="shared"`, default `ui_form`. Indices map to the catalog
the way Mongo collections do; the Tables Selector, indexing pipeline, and
agent prompt templating all work unchanged.

**Catalog discovery** (`get_schemas()` / `get_tables()`):

- List non-system indices (`_cat/indices`, drop `.`-prefixed and hidden
  indices) plus aliases. An optional `index_pattern` config (comma-separated
  globs, e.g. `logs-*,orders`) narrows discovery — same idea as Druid's
  `schema` filter. Data streams are deferred to a follow-up (their backing
  `.ds-*` indices are hidden; the stream name is the queryable surface).
- For each index, flatten the mapping into `TableColumn`s with dot paths,
  reusing the MongoDB conventions: `object` fields recurse
  (`customer.tier`), `nested` fields recurse with the `[]` marker plus an
  `array` column for the field itself. Unlike Mongo there is **no sampling**:
  the mapping *is* the schema, so discovery is one cheap call — a real
  advantage over the Mongo client's `$sample` round-trips.
- Mapping type → dtype: `keyword`/`text`/`ip` → `string`; `long`/`integer`/
  `short`/`byte` → `integer`; `double`/`float`/`half_float`/`scaled_float` →
  `number`; `boolean` → `boolean`; `date`/`date_nanos` → `datetime`;
  `nested` → `array`; anything else → `object`. Keep `metadata_json`
  per table: `{"type": "index" | "alias", "raw_types": {...}}` so `text` vs
  `keyword` (aggregatable or not) survives into the catalog.
- `pks=[TableColumn(name="_id", dtype="string")]`, `fks=[]` — same as Mongo.

### Query interface: JSON envelope over `_search` (primary), SQL escape hatch

`execute_query(query: str)` takes a JSON string, mirroring the Mongo
contract the coder agent already knows:

```json
{
  "index": "orders",
  "query":  { "term": { "status": "active" } },
  "aggs":   { "by_tier": { "terms": { "field": "customer.tier" },
              "aggs": { "revenue": { "sum": { "field": "total" } } } } },
  "size":   100,
  "sort":   [ { "created_at": "desc" } ],
  "_source": ["order_id", "total"]
}
```

- `index` is required; everything else is passed through to `POST
  /{index}/_search` (whitelisted keys: `query`, `aggs`, `size`, `sort`,
  `_source`, `from`, `search_after`, `timeout`). Alternatively
  `{"index": "...", "sql": "SELECT ..."}` posts to the SQL endpoint —
  validated live below; useful because LLMs are strong at SQL, but DSL stays
  primary since SQL has real gaps (nested/multi-valued fields, `text`
  aggregation).
- **Result → DataFrame:**
  - *Hits path* (no `aggs`, or `size > 0`): `pd.json_normalize` over
    `hits[]._source` (dot-path columns match the catalog), plus an `_id`
    column.
  - *Aggs path* (`aggs` present and `size` 0/absent): recursive bucket
    flattener — each nesting level of `terms`/`date_histogram`/`histogram`/
    `range`/`filters` becomes a key column, metric leaves (`value`-shaped and
    `stats`-shaped) become value columns, `doc_count` is always included.
    This flattener is the one genuinely new piece of logic and gets the
    densest unit coverage. Unrecognized agg shapes fall back to one raw JSON
    column rather than erroring.
- **Guardrails:** default `size` 100 when neither `size` nor `aggs` given
  (Mongo parity); cap `size + from` at 10 000 (the engine's
  `max_result_window` — deeper pagination via `search_after` is a follow-up);
  request `timeout` default 60s and `requests` socket timeout ~65s; connect
  timeout 5s (Mongo parity).

### Auth variants & TLS

Config (shared): `host`, `port` (default **9200**), `secure: bool` (HTTPS),
`verify_certs: bool` (default `True`; self-signed demo certs are the norm on
secured dev clusters, so this must be a first-class toggle), optional
`index_pattern`. Also accept a full URL in `host` (Elastic Cloud endpoints
have no separate host/port).

- `opensearch`: `userpass` (default, HTTP basic — what the security plugin
  speaks) and `none` (security disabled / network-gated dev clusters). Both
  `scopes=["system","user"]` except `none` (system only, per repo convention).
- `elasticsearch`: `userpass` (default), `api_key` (single `ApiKey` header
  value — the Elastic Cloud norm), `none` (system only).

Existing credential classes cannot be reused (`SQLCredentials.user/password`
are required, but `none`-auth needs empties), so add small dedicated classes —
field names/descriptions are user-facing form copy.

### Coder guidance (`description` property)

Same shape as `MongodbClient.description`: the CRITICAL RULES block plus 2–3
worked examples. Engine-specific rules the LLM must see:

1. Only use fields present in the schema; never invent fields.
2. **Aggregate/sort on `keyword` fields, not `text`** — if the schema shows a
   `text` field, use its `.keyword` subfield when one exists (surfaced via
   `raw_types` metadata); aggregating on bare `text` fails with fielddata
   errors.
3. Fields under a `nested` type need the `{"nested": {"path": ...}}` query
   wrapper; plain dot-path queries silently match nothing.
4. Valid JSON only (`true`/`false`/`null`); `size: 0` when only `aggs` matter.
5. If unsure of the data shape, use `inspect_data` first (Mongo parity).

---

## Files to create / update (per the add-connection-type skill)

| Layer | File | Change |
|---|---|---|
| Client | `backend/app/data_sources/clients/opensearch_client.py` | `SearchEngineBaseClient(DataSourceClient)` + `OpenSearchClient`; `get_schemas`/`get_tables`, `execute_query`, `test_connection`, `description`, mapping + agg flatteners |
| Client (P2) | same module | `ElasticsearchClient` subclass (SQL path, `ApiKey` header) |
| Config | `backend/app/schemas/data_sources/configs.py` | `OpenSearchConfig`, `OpenSearchCredentials`, `OpenSearchNoAuthCredentials` (+ P2: `ElasticsearchConfig`, `ElasticsearchApiKeyCredentials`) |
| Registry | `backend/app/schemas/data_source_registry.py` | Two entries, `dev_only=True`, `version="beta"`, explicit `client_path` (the contract — never rely on the dynamic fallback), `data_shape="objects"`, `is_document_based=True` |
| Driver | — | none (`requests` already a dependency); no Dockerfile change |
| Icons | `frontend/public/data_sources_icons/opensearch.png`, `elasticsearch.png` | + mapping in `frontend/components/DataSourceIcon.vue` |
| Unit tests | `backend/tests/unit/test_opensearch_client.py` | mock the transport boundary only (fake `requests.Session`, per `test_druid_client.py`) |
| Integration | `backend/tests/integrations/ds_clients.py` | add to `DATA_SOURCES`; `CONTAINER_REGISTRY` entries via `testcontainers[opensearch]` / `[elasticsearch]` (extras exist in the pinned 4.14 line → update the extra list in `backend/pyproject.toml`); seeding needs a per-entry `seed_fn` hook — the current `seed_sql` path is SQLAlchemy-only |
| Feedback loop | `docs/feedback-loops/opensearch-connector.md` | recorded during implementation (template below is pre-validated) |

No frontend form code: `ConnectForm.vue` renders from the config/credentials
schemas via `GET /available_data_sources`.

Unit-test coverage priorities: mapping flattener (object/nested/multi-field),
agg-result flattener (nested terms + metrics, date_histogram, stats,
unknown-agg fallback), envelope validation (missing `index`, non-whitelisted
keys), size cap, auth header construction per variant, `test_connection`
success/failure, `resolve_client_class("opensearch")`, config validation.

---

## Sandbox feasibility — **validated live** (2026-07-07)

The question this answers: *can the sandbox-feedback-loop run OpenSearch and
the app together?* **Yes.** Everything below was executed in a fresh cloud
sandbox; outputs are observed, not hypothetical.

### 0. Docker daemon quirk

The sandbox has Docker 29.3.1 installed but **dockerd is not running**. Start
it with the pre-configured proxy env (image pulls go through the agent proxy):

```bash
HTTPS_PROXY=$DOCKER_HTTPS_PROXY HTTP_PROXY=$DOCKER_HTTPS_PROXY setsid dockerd \
  > /tmp/dockerd.log 2>&1 &
until docker info >/dev/null 2>&1; do sleep 1; done
```

### 1. OpenSearch single-node

```bash
docker run -d --name os-dev -p 9200:9200 \
  -e discovery.type=single-node \
  -e DISABLE_SECURITY_PLUGIN=true \
  -e "OPENSEARCH_JAVA_OPTS=-Xms512m -Xmx512m" \
  opensearchproject/opensearch:2.19.1
```

Observed: healthy in ~15s, **~1.0 GiB RSS** (sandbox has 15 GiB — plenty of
headroom next to the app stack). `curl localhost:9200` returned
`"distribution": "opensearch", "number": "2.19.1"`. No `vm.max_map_count`
issue in this sandbox. Containers can't reach the agent proxy, but OpenSearch
needs no outbound network.

### 2. Seed + probe the three API surfaces the connector uses

Seeded an `orders` index (explicit mapping incl. an object field
`customer.{name,tier}`, 3 docs via `_bulk?refresh=true`). All three probes
passed:

- **Discovery** — `_cat/indices?format=json` listed `orders` (and the
  `.opensearch-observability` system index → confirms the `.`-prefix filter is
  needed); `GET /orders/_mapping` returned the full typed field tree,
  including the nested `customer` properties.
- **Query DSL** — `terms` agg on `status` with a `sum(total)` sub-agg
  returned `active: doc_count 2, revenue 200.5 / cancelled: 1, 15.0` —
  exactly the bucket shape the agg flattener will consume.
- **SQL plugin** (bundled, no install) — `POST /_plugins/_sql` with
  `SELECT status, COUNT(*) AS n, SUM(total) AS revenue FROM orders GROUP BY
  status` returned typed `schema` + `datarows` — the escape hatch works
  out of the box.

### 3. The app next to it

`tools/agent/boot_stack.sh` (+ `seed_org.py`) boots backend :8000 / frontend
:3000 in the sandbox — the standard loop used by existing feedback-loop docs.
The backend connects to `localhost:9200` directly (`no_proxy` covers
localhost). Live UI pass for the implementation PR: create the connection →
Test connection → Tables Selector lists `orders` → prompt that queries it;
screenshots via the **ui-evidence** skill.

### 4. Deterministic legs (no Docker needed)

- **Loop A**: unit tests with the transport faked — runs anywhere.
- **Integration**: `uv run pytest tests/integrations/ds_clients.py -k opensearch`
  via testcontainers once the `CONTAINER_REGISTRY` entry exists (mirrors the
  `postgres:15` pattern; seed with the same `_bulk` payload).

One proxy caveat for **remote** clusters (not the local loop): the sandbox
proxy only supports HTTPS on :443, so an external cluster on :9200 isn't
reachable from a sandbox — Elastic Cloud (:443) is. Local containers are
unaffected. Irrelevant outside sandboxes.

---

## Phasing

1. **OpenSearch connector** — base client + config + registry (`dev_only`),
   unit tests, testcontainers integration, feedback-loop doc, icon. The live
   leg re-runs the probe above through the real client.
2. **Elasticsearch variant** — subclass (+ `api_key` auth, `/_sql` path),
   `elasticsearch:8.x` testcontainers leg, icon.
3. **Polish / follow-ups** — data streams in discovery, `search_after`
   pagination, drop `dev_only`, docs page under `documents/`.

## Open questions

- **Amazon OpenSearch Service (SigV4 auth)?** Common managed deployment; needs
  request signing → would justify pulling in `opensearch-py`/`botocore` later.
  Proposal: ship basic/none first, add a `sigv4` auth variant on demand.
- **Elasticsearch serverless / very old versions**: target ES 7.10+ and 8.x;
  serverless removes `_cat` conveniences (use `GET /_alias` + `_mapping`
  instead) — verify in Phase 2.
- **Row-limit semantics**: org row limits apply to SQL sources via query
  wrapping; for document clients (Mongo today) the cap is the client-side
  `size` default. Follow whatever Mongo does today (`size` cap) for
  consistency, and revisit both together if org limits must bind harder.
