# Feedback Loop — Prometheus connector

Adds a **Prometheus** data source connector: a `DataSourceClient` subclass that
speaks the Prometheus HTTP API (`/api/v1/query`, `/api/v1/query_range`,
`/api/v1/label/__name__/values`, `/api/v1/metadata`, `/api/v1/series`) using
plain `requests`, with Pydantic config/credentials schemas and a `REGISTRY`
entry (beta, `dev_only` while incubating). Prometheus is queried with **PromQL**,
not SQL — the closest existing reference is `posthog_client.py` (HTTP API + a
non-SQL query language + a discovered catalog), *not* `postgresql_client.py`.

## What was added

| Layer | File | Change |
|-------|------|--------|
| Client | `backend/app/data_sources/clients/prometheus_client.py` | New `PrometheusClient(DataSourceClient)` |
| Config | `backend/app/schemas/data_sources/configs.py` | `PrometheusConfig`, `PrometheusNoAuthCredentials`, `PrometheusBasicCredentials`, `PrometheusBearerCredentials` |
| Registry | `backend/app/schemas/data_source_registry.py` | `"prometheus"` entry: explicit `client_path`, `data_shape="tables"`, `version="beta"`, `dev_only=True` |
| Driver | — | none: plain `requests` (already a dependency), no Dockerfile change |
| Icon | `frontend/public/data_sources_icons/prometheus.png` | Prometheus flame mark |
| Unit tests | `backend/tests/unit/test_prometheus_client.py` | 12 tests, `requests` boundary faked |
| Demo stack | `docs/feedback-loops/assets/prometheus-stack/` | compose: Prometheus + Alertmanager + node-exporter + alert/recording rules |

## Design decisions

- **Metric = table.** `get_schemas()` enumerates metric names via
  `GET /api/v1/label/__name__/values`, enriches type/help from
  `GET /api/v1/metadata`, and derives each metric's label set from
  `POST /api/v1/series` (one `match[]` per name, **batched 40/req** with
  `progress_callback`). Each metric becomes a `Table` whose columns are its
  labels plus synthetic `timestamp` + `value`. `data_shape="tables"`,
  `catalog_ownership="shared"`.
- **`execute_query(query, start=None, end=None, step=None, time=None)`.** No
  window → instant query (`/api/v1/query`); `start`+`end` → range query
  (`/api/v1/query_range`, `step` default `60s`). Vector/matrix/scalar results
  all flatten to a tidy `DataFrame`: one column per label + `timestamp` +
  `value`.
- **HTTP method matters.** `/query`, `/query_range`, `/series` use **POST**
  (form-encode large query/`match[]` payloads); the read-only discovery
  endpoints are **GET** (Prometheus answers POST on them with **405** — this
  bit during bring-up and is now covered by a unit test).
- **PromQL, not SQL.** The client's `description`/system prompt ships a PromQL
  cheat-sheet (rate/increase, histogram_quantile, range selectors, `ALERTS`) so
  the agent emits PromQL. Firing alerts are reachable with no Alertmanager via
  the synthetic `ALERTS{alertstate="firing"}` series.
- **Auth**: `none` (network-gated, system scope), `basic` (username/password),
  `bearer` (token). `verify_ssl` toggle; optional `org_id` → `X-Scope-OrgID`
  for multi-tenant Thanos/Cortex/Mimir.

## Environment setup (fresh sandbox)

```bash
cd backend && pip install uv && uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
```

## Loop A — unit validation (no live Prometheus) — PASS

`requests.Session` is faked with a `{path: response}` routing table (records the
HTTP method per endpoint). Covers metric→table shaping (labels + synthetic
cols, metadata type/help, `metric_prefix` filter), GET-vs-POST endpoint
selection, instant/range/scalar flattening (numeric `value`, datetime
`timestamp`), API-error surfacing, bearer/basic auth wiring + `X-Scope-OrgID`,
and `test_connection` success/failure.

```bash
cd backend && export BOW_DATABASE_URL="sqlite:///db/app.db" ENVIRONMENT=development TESTING=true
uv run --extra dev pytest tests/unit/test_prometheus_client.py -q
#  -> 12 passed
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('prometheus'))"
#  -> <class 'app.data_sources.clients.prometheus_client.PrometheusClient'>
uv run --extra dev pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
#  -> 11 passed
```

## Loop B — live confirmation (massive Prometheus + Alertmanager) — PASS

A real stack under `assets/prometheus-stack/`: Prometheus + Alertmanager +
node-exporter, with alert rules (some deliberately firing), recording rules, and
scrape targets that are intentionally down (so `up == 0` / `InstanceDown` fire).

```bash
# start dockerd if needed, then:
docker compose -f docs/feedback-loops/assets/prometheus-stack/docker-compose.yml up -d
until curl -s localhost:9090/-/ready >/dev/null; do sleep 1; done
```

Observed against this stack (Prometheus **3.13.0**):

```
metric names ............ 672        (230 node_*)
total series ............ 2187
firing alerts ........... 9   (InstanceDown x3 [critical], FilesystemFillingUp x3,
                               HighMemoryUsage, HighRequestLatencyDemo, DemoHeartbeat)
alertmanager /api/v2/alerts ... 9
```

Driving the actual `PrometheusClient` against it:

```
test_connection ......... {'success': True, 'message': 'Successfully connected to Prometheus 3.13.0'}
get_schemas ............. 672 metrics in 0.1s
up.columns .............. [app, instance, job, team, tier, timestamp, value]
execute_query('up') ..... 6 rows (3 up=1, 3 down=0), one row per series
ALERTS{alertstate="firing"} ... 9 rows, columns include alertname/severity/value
range query (rate, step=30s) .. matrix exploded to one row per sample
histogram_quantile(0.95, ...) . p95 latency row returned
```

All live checks passed — see the connector's own `system_prompt()` for the
PromQL patterns the agent is told to use.

## UI evidence (live BOW stack)

`tools/agent/boot_stack.sh` + the Prometheus stack above, driven with Playwright
(`tools/agent/prom_ui_flow.mjs`). Screenshots under `media/prometheus/`:

- `01-datasource-grid.png` — Prometheus tile (flame icon) in the connector grid.
- `02-connect-form-filled.png` — schema-generated connect form: Base URL, Verify
  SSL, Tenant/Org ID, Metric Name Filter, and the auth dropdown — every field's
  help text is its Pydantic `description`.
- `03-test-connection-result.png` — **"Connected successfully. Found 672 tables."**
- `05-metrics-schema.png` — the metric-as-table selector: **"Showing 1-100 of 672"**,
  paginated, each metric expandable to its label columns.

11 focused metrics were activated for querying (`up`, `ALERTS`,
`node_memory_*`, `node_filesystem_*`, `node_cpu_seconds_total`,
`prometheus_http_requests_total`, `prometheus_http_request_duration_seconds_bucket`,
`job:up:count`).

## Alertmanager — scope note

Alertmanager runs in the demo stack (and Prometheus forwards alerts to it), but
the **connector deliberately does not integrate it**. "Which alerts are firing"
is answerable from Prometheus's own API — the `ALERTS` PromQL series and
`/api/v1/rules` / `/api/v1/alerts` — with no Alertmanager. Alertmanager's own
API (`/api/v2/alerts`, `/api/v2/silences`) is notification routing/silencing/
inhibition, an ops-management surface that maps poorly onto the analytics agent.
If wanted later it should be its **own** registry entry, not an auth variant or
toggle on Prometheus.

## Live LLM conversations (Claude 4.5 Haiku) — PASS

An Anthropic provider + `claude-haiku-4-5-20251001` default was configured
(`tools/agent/setup_haiku_llm.py`) and two real conversations were driven end to
end over the Prometheus source (Playwright, `tools/agent/prom_convo.mjs`). The
agent plans → writes PromQL → executes it against the live stack → answers, over
multiple Haiku iterations per turn (17 Anthropic calls across the two runs).

- `10-conversation-targets.png` — *"Which scrape targets are currently down?"* →
  Haiku queries the `up` table and returns exactly the 3 down targets
  (`api` @ .1:9000, `api` @ .2:9000, `worker` @ .3:9000) as a table + summary.
- `11-conversation-alerts.png` — *"How many alerts are firing, by severity?"* →
  Haiku queries the `ALERTS` table and groups all **9 firing alerts**: Critical 3
  (InstanceDown), Warning 5 (FilesystemFillingUp ×3, HighRequestLatencyDemo,
  HighMemoryUsage), Info 1 (DemoHeartbeat) — matching the alert rules exactly.

This confirms the full path: connector schema → agent planner → PromQL over the
HTTP API → result table → natural-language answer, with alerts reached via the
`ALERTS` series (no Alertmanager integration needed, per the scope note above).
