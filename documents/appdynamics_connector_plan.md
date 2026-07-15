# AppDynamics Connector — Implementation Plan

Status: **planning only — no implementation yet**

Goal: add a first-class AppDynamics (now **Splunk AppDynamics**) data source
connector in the `infra` family alongside Splunk / Prometheus / Jaeger /
Zabbix — an admin connects a Controller, the platform indexes applications,
business transactions and metric paths as tables, and the agent queries APM
metrics, events and health-rule violations at runtime.

This plan mirrors `documents/servicenow_connector_plan.md`, which documents
the connector framework in detail. Only AppDynamics-specific decisions are
spelled out here.

---

## 1. How connectors work today (context)

A connector is four pieces, all registry-driven (see the ServiceNow plan §1
for the long version):

| Piece | Location |
|---|---|
| Client | `backend/app/data_sources/clients/appdynamics_client.py` — `AppDynamicsClient(DataSourceClient)` |
| Config / credentials schemas | `backend/app/schemas/data_sources/configs.py` — Pydantic models with `ui:type` extras; frontend forms render automatically |
| Registry entry | `backend/app/schemas/data_source_registry.py` — `DataSourceRegistryEntry`, `category="infra"`, explicit `client_path` |
| Icon | `frontend/public/data_sources_icons/appdynamics.png` |

No bespoke frontend or API work is needed. The AI layer works generically off
`get_schemas()` + `execute_query()` + `description` (the coder prompt injects
each client's `description`, which is where the query-dialect guidance in
`system_prompt()` reaches the LLM — `backend/app/ai/agents/coder/coder.py`).

Closest reference implementations:
- `servicenow_client.py` — REST connector with a **JSON query spec** instead
  of a query language, structured HTTP error mapping, curated + discover-all
  schema modes. **Best overall template.**
- `prometheus_client.py` — observability connector where each metric becomes
  a table; multiple auth variants (`none`/`basic`/`bearer`).
- `splunk_client.py` — enterprise-licensed infra connector with a
  `discovery_window_days` config.

## 2. AppDynamics API surface (what we'll build on)

Target the classic **Splunk AppDynamics Controller REST API** (SaaS and
on-prem — identical paths), plus optionally the **Analytics Events API** in a
later phase. Do **not** build against Cisco Cloud Observability / Cisco
Observability Platform APIs — that product line is being wound down in favor
of Splunk Observability Cloud. The Controller API surface is unchanged by the
Cisco→Splunk migration (`/controller/rest/...` paths, `*.saas.appdynamics.com`
hosts); docs now live on help.splunk.com.

All Controller endpoints return XML by default — always pass `output=JSON`.

**Application model (read-only):**
- `GET /controller/rest/applications` — business applications
- `GET /controller/rest/applications/{app}/business-transactions`
- `GET /controller/rest/applications/{app}/tiers`, `/nodes`, `/backends`

**Metrics:**
- `GET /controller/rest/applications/{app}/metric-data` with:
  - `metric-path` — pipe-delimited tree path, wildcards `*` supported
    (e.g. `Business Transaction Performance|Business Transactions|*|*|Average Response Time (ms)`)
  - `time-range-type` = `BEFORE_NOW` | `BEFORE_TIME` | `AFTER_TIME` |
    `BETWEEN_TIMES`, with `duration-in-mins` and/or `start-time`/`end-time`
    (epoch **milliseconds**)
  - `rollup=true|false` — true (default) returns one aggregated point;
    false returns the time series
- `GET /controller/rest/applications/{app}/metrics?metric-path=...` — browse
  the metric tree (folder-by-folder); used for discovery.
- Server-side granularity: ranges ≤ 24h (within ~8 days) return 1-minute
  points; longer ranges return hourly rollups. Not something we control.

**Events / health rules:**
- `GET /controller/rest/applications/{app}/events` — requires
  `time-range-type` (+ duration/start/end), `event-types`, `severities`
  (`INFO,WARN,ERROR`). **Hard cap of 600 events per call** — paginate by
  slicing time windows.
- `GET /controller/rest/applications/{app}/problems/healthrule-violations` —
  same time-range parameter model.

**Auth options:**
- **API Clients (OAuth2 client-credentials)** — `POST
  /controller/api/oauth/access_token` with
  `grant_type=client_credentials&client_id=<name>@<account>&client_secret=...`;
  returns a short-lived JWT (**default 5 minutes**) used as
  `Authorization: Bearer`. The client must refresh proactively.
- **Basic auth** — username format `<user>@<account>`. Unreliable on SaaS:
  users behind the Cisco/Splunk IdP (SSO) cannot use basic auth at all.

**Analytics Events API (ADQL) — Phase 2:** separate Events Service endpoint
(SaaS regions: `analytics.api.appdynamics.com`,
`fra-ana-api.saas.appdynamics.com`, `syd-ana-api.saas.appdynamics.com`;
on-prem port 9080), `POST /events/query` with ADQL
(`SELECT ... FROM transactions ...`), headers `X-Events-API-AccountName`
(**global** account name from the License page) + `X-Events-API-Key` +
`Content-Type: application/vnd.appd.events+json;v=2`. Limits: 10K results per
query (scroll mode for bulk), ~450 req/min per account.

**Rate limits:** no published hard Controller limit, but Splunk guidance says
keep metric data points per call under ~50K and poll on ~5-minute cadences;
SaaS controllers can throttle.

**Dependencies:** none new — plain `requests`, matching PostHog/ServiceNow.
The `appd-api` / community SDKs are unmaintained; skip them.

## 3. Design decisions

### 3.1 Query interface for the agent

AppDynamics has no SQL and its "query" is really *endpoint + parameters*, so —
exactly like ServiceNow — `execute_query()` accepts a **JSON string spec**
with an `endpoint` discriminator, documented in `system_prompt()`:

```json
{
  "endpoint": "metric-data",
  "application": "ECommerce",
  "metric_path": "Business Transaction Performance|Business Transactions|*|*|Average Response Time (ms)",
  "duration_mins": 1440,
  "rollup": false
}
```

```json
{
  "endpoint": "events",
  "application": "ECommerce",
  "event_types": "APPLICATION_ERROR,POLICY_OPEN_CRITICAL",
  "severities": "ERROR,WARN",
  "duration_mins": 240
}
```

Supported `endpoint` values in Phase 1 and the DataFrame each returns:

| endpoint | maps to | DataFrame shape |
|---|---|---|
| `applications` | `/rest/applications` | one row per application (id, name, description) |
| `business-transactions` | `/rest/applications/{app}/business-transactions` | one row per BT (id, name, tier, entryPointType) |
| `tiers` / `nodes` / `backends` | corresponding endpoints | one row per entity |
| `metric-data` | `/rest/applications/{app}/metric-data` | **long format**: `metric_name, metric_path, start_time, value, min, max, count, sum` — one row per (metric, timestamp); flattens the nested `metricValues` arrays |
| `metric-browse` | `/rest/applications/{app}/metrics` | one row per child of a metric-tree folder (for the agent to explore paths) |
| `events` | `/rest/applications/{app}/events` | one row per event (time, type, severity, summary, affected entities) |
| `healthrule-violations` | `/rest/applications/{app}/problems/healthrule-violations` | one row per violation |

Also accept `start_time`/`end_time` (ISO 8601 strings — the client converts
to epoch-millis and picks the right `time-range-type`) as an alternative to
`duration_mins`. Defaults: `duration_mins=60`, `rollup=false` for
`metric-data` (a time series is almost always what an analytics question
wants), sane `severities`/`event_types` defaults for `events`.

Rejected alternatives: raw URL passthrough (no guardrails, no prompting
story); inventing a SQL dialect over the metric tree (complex, lossy);
one-method-per-endpoint on the client (the coder calls `execute_query`
generically — a spec keeps the single entry point).

### 3.2 Schema discovery — what is a "table" for an APM tool?

The catalog/prompt layer expects `Table` objects. AppDynamics data is a
metric tree, not tables, so we synthesize a small, stable set of **virtual
tables** (the Prometheus connector set the precedent of adapting
non-tabular sources):

1. **One catalog table per entity collection** — `applications`,
   `business_transactions`, `tiers`, `nodes`, `backends`, `events`,
   `healthrule_violations` — with fixed, documented columns matching the
   DataFrame shapes above. These are cheap to build (entity endpoints, a
   handful of requests) and give the agent concrete names to select on.
2. **One `metric_data` table** with the fixed long-format columns, whose
   description teaches that rows are selected via `metric_path`, not WHERE
   clauses.
3. **Metric-path guidance in the prompt, not the catalog.** Do NOT enumerate
   the metric tree into thousands of tables (a mid-size controller has
   10k–100k+ metric paths; it would blow up the catalog and the prompt).
   Instead `system_prompt()` documents the well-known top-level trees
   (`Overall Application Performance|...`, `Business Transaction
   Performance|Business Transactions|<tier>|<bt>|...`, `Errors|...`,
   `Application Infrastructure Performance|...`) plus the standard leaf
   metrics (Average Response Time (ms), Calls per Minute, Errors per Minute,
   …), wildcard usage, and tells the agent to use the `metric-browse`
   endpoint when unsure of a path.

Discovery cost at connect/index time: 1 request for applications + up to
`N_apps` requests for BTs/tiers (bounded by an `applications` config filter,
see below). Entity row *values* (application names, BT names) are embedded in
the table descriptions so the LLM can reference real names without a runtime
probe — same trick the Prometheus connector uses for label values.

Config knob: `AppDynamicsConfig.applications` — optional comma-separated
allowlist of application names to index (empty = all). Keeps huge controllers
tractable and mirrors `ServiceNowConfig.tables`.

### 3.3 Auth

**Phase 1: OAuth2 API Client (`client_id` + `client_secret`) as the default
variant, basic auth (`username@account` + password) as a secondary variant.**

- API Clients are the only reliable path on SaaS (SSO users can't basic-auth)
  and the officially recommended one. The client caches the bearer token and
  re-fetches on expiry or 401 (default lifetime is 5 minutes, so refresh
  handling is mandatory, not optional).
- Registry `AuthOptions(default="api_client", by_auth={"api_client": ...,
  "basic": ...})`, both with `scopes=["system", "user"]` — the per-user
  credential overlay comes free from the framework (AppDynamics API Clients
  are account-scoped rather than user-scoped, so per-user matters less than
  for ServiceNow, but it costs nothing to declare).

Schemas (in `configs.py`):

```python
class AppDynamicsConfig(BaseModel):
    controller_url: str   # https://<tenant>.saas.appdynamics.com or on-prem URL
    account: str          # controller account name (SaaS tenant / "customer1")
    applications: Optional[str] = None  # comma-separated allowlist
    verify_ssl: bool = True

class AppDynamicsApiClientCredentials(BaseModel):
    client_id: str        # API Client name (client is combined with account)
    client_secret: str    # ui:type password

class AppDynamicsBasicCredentials(BaseModel):
    username: str         # without @account — client appends it
    password: str         # ui:type password
```

Constructor kwargs of `AppDynamicsClient.__init__` must match the union of
these field names exactly (`construct_client` passes merged config+creds by
name — the documented ServiceNow gotcha).

### 3.4 Result semantics

- `metric-data` responses nest `metricValues` per metric — flatten to long
  format (§3.1) so pandas groupbys/pivots work naturally; document the shape
  in `system_prompt()` with a worked example.
- Timestamps: convert epoch-millis to timezone-aware UTC `datetime` columns.
- Empty metric results (`metricValues: []` or a `METRIC DATA NOT FOUND`
  placeholder name) are common with a mistyped path — detect and raise an
  actionable error suggesting `metric-browse`, instead of returning an empty
  DataFrame the agent will misread as "no traffic".
- Events: enforce/document the 600-event cap; if a response hits 600, warn in
  the error/log and suggest narrowing the window.
- Structured HTTP error mapping as in `servicenow_client._get`: 401 → expired
  token/bad credentials (re-auth once, then fail with message), 403 →
  API Client lacks read scope, 404 → bad controller URL or unknown
  application, 429/throttle → rate-limited, suggest fewer/longer windows.

### 3.5 test_connection()

1. Fetch an OAuth token (validates client_id/secret/account).
2. `GET /controller/rest/applications?output=JSON` (validates URL + read
   permission).
3. Return `{"success": True, "message": "Connected — N applications visible"}`
   or the mapped actionable error.

## 4. File-by-file work plan (Phase 1)

1. `backend/app/schemas/data_sources/configs.py` — add `AppDynamicsConfig`,
   `AppDynamicsApiClientCredentials`, `AppDynamicsBasicCredentials` (with
   `ui:type` extras for form rendering).
2. `backend/app/schemas/data_source_registry.py` — add `"appdynamics"`
   `DataSourceRegistryEntry`: `title="AppDynamics"`, `category="infra"`,
   `version="beta"`, `config_schema=AppDynamicsConfig`, two auth variants
   (default `api_client`), **explicit
   `client_path="app.data_sources.clients.appdynamics_client.AppDynamicsClient"`**
   (title-casing would produce `AppdynamicsClient` — the known
   resolve_client_class pitfall). Decide whether
   `requires_license="enterprise"` applies (Splunk/Zabbix precedent — open
   question §8).
3. `backend/app/data_sources/clients/appdynamics_client.py` — the client:
   token manager (fetch/cache/refresh), `connect()` contextmanager yielding an
   authed `requests.Session`, `_get()` with structured error mapping,
   `test_connection()`, `get_schemas()`/`get_schema()` (virtual tables per
   §3.2), `prompt_schema()` via `ServiceFormatter`, `execute_query()` (JSON
   spec → endpoint dispatch → DataFrame), `system_prompt()` (dialect doc:
   spec format, metric-path trees, wildcards, time ranges, data-shape
   warnings), `description` property (summary + system_prompt).
4. `frontend/public/data_sources_icons/appdynamics.png` — icon.
5. `backend/tests/unit/test_appdynamics_client.py` — mock `requests` at the
   HTTP boundary (ServiceNow/Splunk test pattern): token refresh on expiry,
   401-once retry, spec parsing/validation, metric flattening, empty-metric
   and 600-event-cap handling, `get_schemas` with/without `applications`
   filter.
6. `backend/tests/integrations/ds_clients.py` — optional live target (env-var
   gated) for a real controller.
7. `CHANGELOG.md` — entry under Unreleased.
8. Docs: short setup page (creating an API Client in the Controller UI,
   required read permissions) — mirroring existing connector docs.

No frontend or API-route changes.

## 5. Phasing

- **Phase 1 (this plan):** Controller REST read-only — applications, BTs,
  tiers/nodes/backends, metric-data, metric-browse, events, health-rule
  violations. OAuth API Client + basic auth. Curated virtual-table schema.
- **Phase 2:** Analytics Events API (ADQL) as an `analytics` endpoint in the
  query spec — needs two extra config/credential fields (analytics endpoint
  URL or region, global account name, Events API key). ADQL is SQL-like, so
  prompting quality should be strong. Also: request-snapshots endpoint,
  per-user delegated flows if demanded.
- **Phase 3 (only if demand):** dashboards/export APIs, server-visibility
  (machine) metrics beyond the metric tree.

## 6. Development & test environment

- **No free permanent dev instance exists** (AppDynamics killed free trials
  post-acquisition for most regions; a Splunk AppDynamics SaaS trial via
  sales, or a customer/partner controller, is the realistic option). This
  makes the mocked unit-test tier the primary safety net — invest in
  realistic fixture payloads (the JSON response shapes are stable and well
  documented on help.splunk.com).
- Tiers per repo convention: unit tests with mocked `requests` (always run) +
  live integration target in `tests/integrations/ds_clients.py` gated on
  `APPDYNAMICS_CONTROLLER_URL`/`APPDYNAMICS_CLIENT_ID`/... env vars.
- Manual E2E when a controller is available: connect via UI → index → ask the
  agent "what's the average response time trend for <app> over the last day"
  and "any critical health rule violations this week" → verify a feedback-loop
  doc in `docs/feedback-loops/appdynamics-connector.md` like the ServiceNow one.

## 7. Appendix — creating the API Client (admin setup)

In the Controller UI: gear icon → Administration → API Clients → Create.
Set client name + secret, add role(s) with read access (e.g. *Applications &
Dashboards Viewer* or a custom read-only role), and set a token lifetime.
The connector needs: client name, secret, account name (License page), and
the controller URL. Document that API-generated tokens default to 5-minute
expiry — the connector refreshes automatically.

## 8. Open questions

1. **`requires_license="enterprise"`?** Splunk and Zabbix connectors are
   enterprise-gated; Prometheus/Jaeger are not. Product call on where
   AppDynamics lands.
2. **Metric-path value embedding depth** — embed BT/tier names per
   application in table descriptions (richer prompts) vs. keeping
   descriptions static and relying on the `business-transactions` +
   `metric-browse` endpoints at runtime (smaller prompt). Proposed: embed up
   to a cap (~50 names per app), fall back to runtime browsing.
3. **Default time window** for `metric-data` when the agent omits one —
   60 min proposed; could align with Splunk connector's
   `discovery_window_days`-style config instead.
4. **Phase 2 Analytics credentials placement** — same connection (extra
   optional fields) vs. a separate `appdynamics_analytics` connector type.
   Proposed: same connection, optional fields.
