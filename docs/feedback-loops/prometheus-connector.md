# Feedback Loop — Prometheus connector

Adds a **Prometheus** data source connector: a `DataSourceClient` subclass that
speaks the Prometheus HTTP API (`/api/v1/query`, `/api/v1/query_range`,
`/api/v1/label/__name__/values`, `/api/v1/metadata`, `/api/v1/series`) using
plain `requests`, with Pydantic config/credentials schemas and a `REGISTRY`
entry (beta, `dev_only` while incubating). Prometheus is queried with **PromQL**,
not SQL — the closest existing reference is `posthog_client.py` (HTTP API + a
non-SQL query language + a discovered catalog), *not* `postgresql_client.py`.

> Status: **planning**. The environment scaffolding for Loop B is validated
> (a real Prometheus runs in the sandbox and answers every endpoint the client
> needs — see the observed output below). The connector code itself is not yet
> written; Loop A assertions and the `## What was added` table describe the
> target and will be confirmed FAIL→PASS during implementation.

## What will be added

| Layer | File | Change |
|-------|------|--------|
| Client | `backend/app/data_sources/clients/prometheus_client.py` | New `PrometheusClient(DataSourceClient)` (+ `PrometheusClient` explicit `client_path`) |
| Config | `backend/app/schemas/data_sources/configs.py` | `PrometheusConfig`, `PrometheusNoAuthCredentials`, `PrometheusBasicCredentials`, `PrometheusBearerCredentials` |
| Registry | `backend/app/schemas/data_source_registry.py` | `"prometheus"` entry: explicit `client_path`, `data_shape="tables"`, `version="beta"`, `dev_only=True` |
| Driver | — | none: plain `requests` (already a dependency), no Dockerfile change |
| Icon | `frontend/public/data_sources_icons/prometheus.png` | Official Prometheus mark (`.png`, resolved by convention in `DataSourceIcon.vue`) |
| Unit tests | `backend/tests/unit/test_prometheus_client.py` | `requests` boundary mocked (mirrors `test_druid_client.py`) |
| Integration | `backend/tests/integrations/ds_clients.py` | `prometheus` in `DATA_SOURCES`; `CONTAINER_REGISTRY["prometheus"]` (`prom/prometheus` testcontainer) |

## Design decisions

- **Metric = table.** `get_schemas()` enumerates metric names via
  `GET /api/v1/label/__name__/values`, enriches type/help from
  `GET /api/v1/metadata`, and derives each metric's label set from
  `GET /api/v1/series?match[]=<metric>`. Each metric becomes a `Table` whose
  `columns` are its labels plus synthetic `timestamp` and `value` columns. This
  reuses the existing schema-indexing pipeline and gives the agent real metric
  discovery (`data_shape="tables"`, `catalog_ownership="shared"`). Enumeration
  is `progress_callback`-aware because `/series` fan-out is the slow part on big
  instances; an optional metric-name prefix/`match` filter in the config bounds
  it.
- **`execute_query(query, start=None, end=None, step=None)`.** Instant query
  (`/api/v1/query`) by default; range query (`/api/v1/query_range`) when a
  `start`/`end` window is supplied. Prometheus's vector/matrix JSON flattens to
  a tidy `pandas.DataFrame`: one column per label + `timestamp` + `value`. The
  base class supplies the `query` alias and the async wrappers.
- **PromQL, not SQL.** The client's `description`/system prompt ships a PromQL
  cheat-sheet (rate/sum/histogram_quantile, range selectors, `ALERTS`) so the
  agent emits valid PromQL instead of SQL. This is the single most important
  correctness detail.
- **Auth**: `none` (network-gated `:9090`, system scope only), `basic`
  (username/password behind a reverse proxy), `bearer` (`Authorization: Bearer`,
  common on hosted Prometheus). `verify_ssl` toggle for self-signed certs;
  optional `org_id` sent as the `X-Scope-OrgID` header for multi-tenant
  Thanos/Cortex/Mimir back-ends.

## Environment setup (fresh sandbox)

```bash
cd backend
pip install uv
uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db"
mkdir -p db
```

## Loop A — unit validation (no live Prometheus) — PLANNED

Self-contained: `requests.request` (or the client's `requests.Session`) is
monkeypatched at the module boundary with a `{(method, path): response}` routing
table, exactly like `test_druid_client.py` fakes `pydruid`. Target coverage:

- URL/auth/TLS construction per variant (none / basic / bearer), `X-Scope-OrgID`
  header when `org_id` is set.
- `get_schemas()` metric→`Table` shaping: `__name__` enumeration, metadata
  type/help enrichment, label→column derivation, `timestamp`/`value` synthetic
  columns, prefix filter honored.
- `execute_query` dispatch: no window → instant (`/api/v1/query`); window →
  range (`/api/v1/query_range`); vector and matrix results both flatten to a
  DataFrame with the expected columns.
- `test_connection()` success / failure (healthy vs unreachable / 401).
- Registry wiring: `resolve_client_class('prometheus')` imports the class.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/unit/test_prometheus_client.py -q          # target: all pass
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('prometheus'))"
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q   # generic flows stay green
```

## Loop B — live confirmation (real Prometheus in the sandbox) — VALIDATED (infra)

The sandbox has Docker but **dockerd is not started by default**; boot it, then
run a single Prometheus. Its default config self-scrapes `localhost:9090`, so
after one scrape interval (~15s) it exposes 300+ real metrics with **no extra
seeding required**.

```bash
# start the daemon if it isn't running
until docker info >/dev/null 2>&1; do sudo -n dockerd >/tmp/dockerd.log 2>&1 & sleep 2; done

docker run -d --name prom-dev -p 9090:9090 prom/prometheus:latest
until curl -s localhost:9090/-/ready >/dev/null 2>&1; do sleep 1; done
sleep 15   # let the self-scrape populate the TSDB
```

Confirm every endpoint the client depends on (raw curl — proves the API surface
before the client exists):

```bash
curl -s localhost:9090/-/healthy
curl -s 'localhost:9090/api/v1/label/__name__/values'                 # metric catalog
curl -s 'localhost:9090/api/v1/metadata?limit=3'                       # type/help enrichment
curl -s 'localhost:9090/api/v1/query?query=up'                         # instant query
curl -s "localhost:9090/api/v1/query_range?query=sum(rate(prometheus_http_requests_total[1m]))&start=$(date -d '-2 min' +%s)&end=$(date +%s)&step=30"
```

**Observed (PASS), abridged — captured in this sandbox on prom/prometheus:latest:**

```
Prometheus Server is Healthy.
# /api/v1/label/__name__/values  -> status: success, 320 metric names
#   e.g. up, go_gc_duration_seconds, prometheus_http_requests_total, process_*
# /api/v1/query?query=up
{'__name__': 'up', 'app': 'prometheus', 'instance': 'localhost:9090', 'job': 'prometheus'}  ['...', '1']
# /api/v1/metadata (sample)
process_network_transmit_bytes_total -> type=counter, help="Number of bytes sent ..."
# /api/v1/query_range sum(rate(prometheus_http_requests_total[1m]))
status success, 1 series, values=[[<ts>, '0.0223']]
```

Once the client lands, the live leg re-runs through it (target shape):

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run python - <<'PY'
from app.data_sources.clients.prometheus_client import PrometheusClient
c = PrometheusClient(base_url="http://localhost:9090")
print(c.test_connection())
tables = c.get_schemas()
print("metrics discovered:", len(tables))
print(tables[0].name, [f"{col.name}:{col.dtype}" for col in tables[0].columns][:6])
print(c.execute_query('up'))                                   # instant
print(c.execute_query('sum(rate(prometheus_http_requests_total[5m]))',
                       start="-10m", end="now", step="1m"))     # range
PY
```

Teardown:

```bash
docker rm -f prom-dev
```

## Alertmanager — scope note

**Alertmanager is out of scope for the initial connector.** Two distinct things
often get conflated:

- *Which alerts are firing* is answerable **from Prometheus's own API** — no
  Alertmanager needed. The synthetic `ALERTS` series is queryable via PromQL
  (`ALERTS{alertstate="firing"}`), and `GET /api/v1/rules` / `GET /api/v1/alerts`
  expose alerting-rule state directly. This falls out of the connector for free
  and should be mentioned in the PromQL cheat-sheet.
- *Alertmanager* is a **separate service** with a separate API
  (`/api/v2/alerts`, `/api/v2/silences`, `/api/v2/status`) whose job is
  notification **routing, grouping, silencing, and inhibition** — an ops-management
  surface, not a metrics/analytics one. Modeling silences/routes as "tables" is
  a poor fit for the analytics agent.

Recommendation: ship Prometheus alone; cover alert *visibility* via `ALERTS` +
`/api/v1/rules`. If Alertmanager is wanted later, add it as its **own** entry
(likely `data_shape="objects"` or a tool provider), not as an auth variant or
config toggle on the Prometheus connector. It can be validated the same way —
`docker run -p 9093:9093 prom/alertmanager` is available in this sandbox.

## What this proves / open items

- **Proven now:** a real Prometheus runs in a clean sandbox and answers the
  catalog, metadata, instant-query, and range-query endpoints the connector is
  designed around — so Loop B is executable, not hypothetical.
- **Pending implementation:** the client, schemas, registry entry, icon, and
  tests (the `## What will be added` table). Loop A assertions land with the
  code and must be shown flipping FAIL→PASS.
- **Live UI pass** (per the `add-connection-type` skill): after the code lands,
  boot the full stack (`tools/agent/boot_stack.sh` + `seed_org.py`), create a
  Prometheus connection against `http://localhost:9090`, confirm "Test
  connection" succeeds, the tables selector lists metrics, and a PromQL-backed
  prompt returns data — captured with the **ui-evidence** skill (the connect
  form is schema-generated, so this doubles as review of the config schemas).
