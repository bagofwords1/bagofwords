# Research: AppDynamics connector — `AppDynamicsClient`

Status: **research only — nothing implemented**. This doc captures the API surface,
auth model, bank/privileged-environment constraints, the MCP angle, and a proposed
shape that matches the existing Splunk / ServiceNow / Zabbix connectors. It follows
the `docs/design/zabbix-connector.md` format so it can graduate into an
implementation plan for `.agents/skills/add-connection-type` when green-lit.

## Mission

Let the agent answer questions like "which business transactions degraded in the
last hour", "show health-rule violations for the payments app", "plot average
response time per tier" against a customer's AppDynamics (Cisco / Splunk
AppDynamics) controller — completing the observability trio next to Splunk
(logs), Zabbix (infra monitoring), and ServiceNow (ITSM). Target deployment is
**on-premises controllers inside privileged bank networks**; SaaS controllers
work identically (same API, different base URL).

## Why Zabbix is the template (not Splunk / ServiceNow)

- Like Zabbix, AppDynamics has **no free-form query language** for its core APM
  data. The Controller exposes a fixed set of REST resources (applications,
  business transactions, metric paths, events, health-rule violations,
  snapshots). That maps cleanly onto Zabbix's **fixed virtual-table catalog**
  (`_CATALOG` dict → `method`, declared columns, pks/fks) rather than Splunk's
  SPL-passthrough or ServiceNow's table-metadata introspection.
- Plain `requests`, no vendor SDK, no driver, no Dockerfile change — same as all
  three existing infra connectors. The community Python SDKs
  ([AppDynamicsRESTx](https://github.com/homedepot/AppDynamicsRESTx), the
  unmaintained [AppDynamicsREST](https://github.com/tradel/AppDynamicsREST),
  and [PyAppd](https://github.com/Appdynamics/PyAppd)) are stale or thin
  wrappers over the same REST calls; a dependency buys nothing and adds a
  supply-chain review item for bank security teams.
- The one structural addition vs Zabbix: an **OAuth token lifecycle** (fetch,
  cache, refresh on 401/expiry) inside the client, because the recommended auth
  is an API Client issuing short-lived OAuth tokens (see Authentication).

## API surface (Controller REST API)

All endpoints live under `https://<controller-host>:<port>/controller/`.
**Default response format is XML — every call must pass `output=JSON`.**
Time ranges use `time-range-type` (`BEFORE_NOW`, `BEFORE_TIME`, `AFTER_TIME`,
`BETWEEN_TIMES`) + `duration-in-mins` / `start-time` / `end-time` (epoch ms).

| Resource | Endpoint | Notes |
|---|---|---|
| Applications | `GET /controller/rest/applications` | id, name, description |
| Business transactions | `GET /controller/rest/applications/{app}/business-transactions` | tier, entryPointType |
| Tiers | `GET /controller/rest/applications/{app}/tiers` | agentType, numberOfNodes |
| Nodes | `GET /controller/rest/applications/{app}/nodes` | machine, agent versions |
| Backends | `GET /controller/rest/applications/{app}/backends` | exit points (DBs, queues, HTTP) |
| Metric hierarchy | `GET /controller/rest/applications/{app}/metrics?metric-path=...` | browse the metric tree |
| Metric data | `GET /controller/rest/applications/{app}/metric-data?metric-path=...&rollup=...` | the workhorse; wildcards (`*`) allowed in path segments |
| Events | `GET /controller/rest/applications/{app}/events?event-types=...&severities=...` | requires event-types + severities filters |
| Health-rule violations | `GET /controller/rest/applications/{app}/problems/healthrule-violations` | open/resolved, affected entity |
| Snapshots | `GET /controller/rest/applications/{app}/request-snapshots` | slow/error transaction snapshots; rich filter params |
| Analytics events (ADQL) | `POST <events-service>/events/query` (separate Events Service host, port 9080/443) | ADQL, own API-key auth |

References: [Platform API index](https://help.splunk.com/en/appdynamics-saas/extend-splunk-appdynamics/26.2.0/extend-splunk-appdynamics/splunk-appdynamics-apis/platform-api-index),
[API overview](https://help.splunk.com/en/appdynamics-on-premises/extend-appdynamics/25.7.0/extend-splunk-appdynamics/splunk-appdynamics-apis/overview-of-splunk-appdynamics-apis).

Scope call: **v1 targets the Controller API only.** The Analytics/ADQL Events
Service is a separate host, separate auth (API key), and often not licensed —
defer it (same reasoning as deferring Splunk's non-search endpoints).

## Data model: REST resources → virtual tables

Zabbix-style fixed catalog; `application` is the common FK thread:

- `applications` (pk `id`)
- `business_transactions` (pk `id`, fk → applications, tier name)
- `tiers`, `nodes`, `backends` (pks `id`, fks → applications; nodes fk → tiers)
- `health_rule_violations` (incidentStatus, severity, affectedEntity, start/end)
- `events` (type, severity, summary, occurred-at; query spec must surface
  `event_types` because the API refuses unfiltered calls)
- `metric_data` (metric_path, metric_name, start/end, value min/max/sum/count,
  rollup flag) — parameterized by `metric_path` (wildcards allowed) + time range
- `snapshots` (bt, tier, node, userExperience, duration, error flag)

Query spec (what `execute_query` would accept), mirroring Zabbix's
`{"table": ..., "filters": ..., "limit": ...}` shape:

```json
{
  "table": "metric_data",
  "application": "payments-prod",          // name or id; omitted for `applications`
  "metric_path": "Business Transaction Performance|*|*|Average Response Time (ms)",
  "duration_in_mins": 60,                   // or start_time/end_time (epoch ms)
  "rollup": false,
  "filters": {"severities": "ERROR,WARN"}, // table-specific extras (events, snapshots)
  "limit": 500
}
```

`relative_date_hint` (rendered into every codegen prompt; none of the current
infra connectors use it but AppD earns one):
`"time-range-type=BEFORE_NOW&duration-in-mins=<n>; absolute times are epoch millis"`.

## Authentication (the one real decision)

AppDynamics Controller supports three schemes:

1. **API Client (OAuth 2.0 client-credentials) — recommended default.**
   Admin creates an API Client in the Controller UI (Settings →
   Administration → API Clients) with named roles;
   `POST /controller/api/oauth/access_token` with
   `client_id=<clientName>@<accountName>`, `client_secret`, grant type
   `client_credentials` returns a short-lived bearer token (default expiry is
   minutes — token caching + refresh-on-401 is mandatory, not an optimization).
   Docs: [API Clients](https://docs.appdynamics.com/appd/23.x/latest/en/extend-appdynamics/appdynamics-apis/api-clients).
2. **Basic auth (legacy):** `<username>@<accountName>:<password>`. Still works
   on-prem; banks with local Controller accounts may prefer it for a pilot.
   On-prem single-tenant installs use account name `customer1`.
3. **Temporary access tokens** generated in the UI — not suitable for a stored
   connection (manual expiry), skip.

Proposed registry auth variants (ServiceNow/Zabbix precedent):

- `api_client` (default): `AppDynamicsApiClientCredentials{client_name, client_secret}`
  — scopes `["system", "user"]` (a per-user API client is a legitimate
  bring-your-own-credential story; the Controller enforces the client's roles).
- `userpass`: `AppDynamicsUserPassCredentials{username, password}` — scopes
  `["system", "user"]`, legacy/basic.

Config schema (non-secret, plaintext JSON): `controller_url` (accept bare host,
host:port, or full URL — normalize like `ZabbixClient`), `account_name`
(default `customer1` for on-prem), `verify_ssl: bool`, and `ca_bundle_path`
(copy the `powerbi_report_server_client.py` pattern — see next section).
Delegated three-legged OAuth (ServiceNow-style per-user browser flow) is **not**
applicable: AppD API Clients are client-credential only.

Least-privilege guidance for the doc/registry description: create the API
Client with a **read-only custom role** (Applications & Dashboards Viewer is
the usual baseline); no write scopes are ever needed — the connector only reads.

## Privileged bank environment constraints

- **On-prem controller, internal CA.** `verify_ssl=false` is the existing
  escape hatch, but banks generally mandate verification against an internal
  CA. The only connector today with a `ca_bundle_path` field is PowerBI Report
  Server (`session.verify = ca_bundle_path`) — AppDynamics should ship with it
  from day one rather than forcing `verify_ssl=false`.
- **Egress proxies.** `requests` honors `HTTPS_PROXY`/`REQUESTS_CA_BUNDLE` env
  vars (`trust_env` is never disabled anywhere in the clients), which matches
  how Splunk/ServiceNow/Zabbix are deployed today; no new work, but worth a
  line in the connector docs.
- **Read-only, auditable service identity.** API Clients are first-class
  auditable identities in the Controller — an easier security-review story
  than a shared human account. Token lifetime is short by default, which
  security teams like; the client must handle it transparently.
- **No dangerous query surface.** Unlike Splunk (`index=*` wildcard-search
  restrictions needed a 3-tier fallback), the AppD REST API is read-only GETs
  with server-side result shaping; the main guardrails are result-size caps
  (`MAX_ROWS`-style) and refusing unbounded time ranges by defaulting
  `duration_in_mins`.
- **Licensing tier.** Splunk and Zabbix are in `ENTERPRISE_DATASOURCES`
  (`backend/app/ee/license.py`); AppDynamics is squarely the same buyer
  profile — expect `requires_license="enterprise"`.
- **Rate limits.** The Controller throttles metric-data queries per account
  ([scalability notes](https://community.splunk.com/t5/AppDynamics-Knowledge-Base/Scalability-of-the-AppDynamics-REST-API/ta-p/718188));
  keep schema indexing to entity lists (cheap) and never enumerate the full
  metric tree during `get_schemas` — mirror Zabbix's "fixed catalog + optional
  best-effort enrichment with a hard limit".

## MCP angle

Two directions were checked (bagofwords both exposes an MCP server and
consumes MCP servers as a `type="mcp"` data source / `McpPreset`):

- **No official Cisco/AppDynamics MCP server exists** (as of Aug 2026).
- Community option: [asafkiv/appdynamics-mcp-server](https://github.com/asafkiv/appdynamics-mcp-server)
  — Node/TypeScript, ~30 tools (apps, health rules, BTs, metrics, snapshots,
  RCA, dashboard CRUD), OAuth client-credentials or API key, supports on-prem
  controllers. Two disqualifiers for us: it is **stdio-only** (our `McpClient`
  speaks SSE / streamable HTTP — someone would have to host and wrap it), and
  it is low-maturity third-party code (~5 stars), which is exactly what a bank
  security review rejects. Also, half its tools are *write* operations
  (dashboard/health-rule CRUD) we don't want in scope.
- Verdict: **native connector, not an MCP preset.** The value of AppD data here
  is tabular (metrics, violations, BTs) that the agent aggregates in pandas —
  that's the data-source pattern, not the tool-provider pattern. `custom_api`
  remains the zero-code escape hatch if a customer wants an AppD pilot before
  the native connector ships.

## Files to create / modify when implemented (per `add-connection-type` skill)

1. `backend/app/data_sources/clients/appdynamics_client.py` — `AppDynamicsClient(DataSourceClient)`, sync, `requests`-only, token cache + refresh-on-401.
2. `backend/app/schemas/data_sources/configs.py` — `AppDynamicsConfig`, `AppDynamicsApiClientCredentials`, `AppDynamicsUserPassCredentials`.
3. `backend/app/schemas/data_source_registry.py` — `"appdynamics"` entry: `category="infra"`, explicit `client_path` (convention would mis-derive `AppdynamicsClient`), `credentials_auth` with the two variants, `requires_license="enterprise"`, `version="beta"`.
4. `backend/app/ee/license.py` — append to `ENTERPRISE_DATASOURCES`.
5. `frontend/public/data_sources_icons/appdynamics.png` — icon only; forms are schema-derived, no frontend code.
6. `backend/tests/unit/test_appdynamics_client.py` — Zabbix-style `_FakeSession` at the `requests` boundary (URL normalization, `output=JSON` on every call, token refresh on 401, catalog shape, query dispatch, `test_connection` success/failure incl. bad-credential and TLS messages).
7. `backend/tests/integrations/ds_clients.py` — `DATA_SOURCES` entry (remote-only; no lightweight AppD testcontainer exists — controller is a heavyweight licensed install, so integration runs need a real/lab controller, like ServiceNow).
8. `README.md` connector table + `CHANGELOG.md`.
9. No DB migration (`Connection.type` is a plain string), no new Python deps.

`test_connection` should follow the Zabbix philosophy: authenticate **and**
count applications, because an API Client with no application grants connects
fine but sees an empty world — return an actionable message.

## Open questions for the customer / before implementation

1. Controller version (on-prem 23.x vs 24/25/26.x) — API surface is stable, but
   confirms the docs set to test against.
2. Is the Analytics/Events Service licensed and reachable, or Controller-only?
   (Determines whether ADQL is ever in scope.)
3. Can the bank issue an API Client, or are only local basic-auth accounts
   permitted? (Decides which auth variant the pilot uses.)
4. Internal CA bundle availability for `ca_bundle_path`.

## Scope summary

Read-only, Controller-API-only, fixed virtual-table catalog, two auth variants
(API Client OAuth default, basic legacy), `requests`-only, enterprise-gated,
no MCP dependency. Estimated shape and size: very close to `zabbix_client.py`
(~450 lines) plus ~60 lines of token-lifecycle code.
