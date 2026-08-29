# Research: AppDynamics connector — `AppDynamicsClient`

Status: **IMPLEMENTED** (beta) — see `docs/feedback-loops/appdynamics-connector.md`
for the full sandbox verification (real LLM + Playwright, service-map and
metric prompts against the `tools/appdynamics/` simulator).
Originally a research doc; it captures the API surface,
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

**Confirmed target (customer): on-prem Controller 21.4, username/password auth.**
The authoritative doc set is the archived 21.x documentation
(`docs.appdynamics.com/appd/21.x`) — ignore anything newer in the current
Splunk-branded 26.x docs. Every endpoint and both auth schemes below exist in
21.4 (the REST surface has been stable since the 4.x era; API Clients/OAuth are
documented in 21.x). Note for the record: 21.4 (April 2021) is past end of
support — don't build anything that assumes an upgrade.

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
- With basic auth as the confirmed default (see Authentication), the client is
  structurally identical to Zabbix's: stateless authed GETs. The OAuth token
  lifecycle (fetch, cache, refresh on 401/expiry) exists only behind the
  secondary `api_client` variant.

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

Zabbix-style fixed catalog of **ten virtual tables**; `application` is the
common FK thread. Two groups with different indexing behavior:

**Topology (indexed eagerly — rows fetched at `get_schemas` time, cheap list
calls only):**

- `applications` (pk `id`; name, description)
- `tiers` (pk `id`; name, agentType, numberOfNodes; fk → applications)
- `nodes` (pk `id`; name, machineName, agentVersion; fks → tiers, applications)
- `backends` (pk `id`; name, exitPointType — DB/queue/HTTP; fk → applications)
- `business_transactions` (pk `id`; name, tierName, entryPointType; fk → applications)
- `service_flows` (from_tier fk → tiers, to_target, to_type `tier|backend`,
  exit_type HTTP/JDBC/JMS/…, application_id fk) — **the service map as an edge
  list**, derived at index time. There is no official flow-map endpoint (the UI
  uses unsupported session-auth `/controller/restui/*` — do not depend on it);
  instead, enumerate metric *names* (not data) under
  `Overall Application Performance|<tier>|External Calls|` via the
  metrics-browse endpoint — one cheap call per tier. Each child name encodes an
  edge (`Call-HTTP to Discovered backend call "X"`, calls to other tiers,
  cross-app flows if configured); parse target + exit type from the name.
  Because `prompt_schema` renders indexed rows via `ServiceFormatter`, the full
  dependency graph lands in the coder agent's context automatically — no
  platform changes; `system_prompt()` explains how to join it with
  tiers/backends for upstream/downstream questions. Optional query-time
  enrichment: calls-per-minute metric-data as edge weight. Caveat: an edge
  exists only if calls occurred in the retention window (same limitation as
  AppD's own flow map).

**Activity (declared in the catalog with fixed column shapes; rows fetched only
at query time with a time range — never at index time):**

- `metric_data` (metric_path, metric_name, startTime, value/min/max/sum/count)
  — parameterized by `metric_path` (wildcards allowed) + time range
- `events` (id, type, severity, summary, eventTime; the API refuses unfiltered
  calls, so the query spec must surface `event_types`/`severities` with defaults)
- `health_rule_violations` (id, name, severity, incidentStatus,
  affectedEntityType/Name, startTime, endTime)
- `snapshots` (requestGUID, businessTransaction, tier, node, userExperience
  NORMAL/SLOW/ERROR, durationMs, errorOccurred, timestamp)

For a typical bank estate (~tens of apps) the indexed catalog is a few hundred
to a few thousand entity rows — names and ids only, no metrics — plus the four
activity-table definitions. Fast to refresh, safely under Controller rate
limits.

**Deliberately not indexed: the metric tree.** The hierarchy under
`Business Transaction Performance|...` / `Application Infrastructure
Performance|...` is tens of thousands of paths and the expensive part of the
API. Instead `system_prompt()` teaches the standard path patterns and wildcards
(e.g. `Business Transaction Performance|*|*|Average Response Time (ms)`,
`Application Infrastructure Performance|<tier>|Hardware Resources|CPU|%Busy`),
and the indexed BT/tier/node names supply the concrete path segments — the
Splunk "thin sourcetypes, agent self-discovers detail" philosophy. The
`metrics` (hierarchy-browse) endpoint stays available at query time as an
explicit `metric_browse` escape hatch if the agent needs to discover a path.

**Coverage check — in vs out (v1):**

| Included | Excluded (and why) |
|---|---|
| APM topology, BTs, metrics, events, violations, snapshots (all above) | Analytics/ADQL — separate Events Service host + license |
| | EUM / Browser & Mobile RUM — separate license, separate metric namespaces (paths still reachable via `metric_data` if licensed) |
| | Database Visibility (`/controller/rest/databases`) — only if the bank licenses DBmon; add later as `databases`/`db_servers` tables (open question below) |
| | Dashboards, policies, actions, config CRUD — write/admin surface, out of scope for a read-only connector |

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

## Authentication

**Decision (customer-confirmed): username/password (HTTP Basic) is the default
variant.** The Controller expects the username qualified with the account name:
`<username>@<accountName>` (on-prem single-tenant = `customer1`), password as-is
— i.e. `requests` `auth=(f"{username}@{account_name}", password)`, the exact
Splunk-userpass pattern with a username transform. Basic auth works on every
Controller REST endpoint in 21.4 and needs **no token lifecycle at all**, which
removes the one structural addition this connector had over the Zabbix template.

Requirements for the bank's service account:
- A dedicated read-only service account with the **Applications & Dashboards
  Viewer** role (or a custom role granting view on the relevant applications).
  An account with no application grants authenticates fine but sees an empty
  world — `test_connection` must count applications and say so.
- Ask about password-rotation policy (stored credentials are Fernet-encrypted,
  but rotation means re-entering them in the connection form) and lockout
  policy (repeated 401s from a stale password could lock the account — the
  client should not retry on 401 with basic auth).

AppDynamics Controller supports three schemes overall:

1. **API Client (OAuth 2.0 client-credentials) — kept as a secondary variant.**
   Admin creates an API Client in the Controller UI (Settings →
   Administration → API Clients) with named roles;
   `POST /controller/api/oauth/access_token` with
   `client_id=<clientName>@<accountName>`, `client_secret`, grant type
   `client_credentials` returns a short-lived bearer token (default expiry is
   minutes — token caching + refresh-on-401 is mandatory, not an optimization).
   Docs: [API Clients](https://docs.appdynamics.com/appd/23.x/latest/en/extend-appdynamics/appdynamics-apis/api-clients).
2. **Basic auth — the default here** (see decision above).
3. **Temporary access tokens** generated in the UI — not suitable for a stored
   connection (manual expiry), skip.

Registry auth variants (ServiceNow/Zabbix precedent):

- `userpass` (default): `AppDynamicsUserPassCredentials{username, password}` —
  scopes `["system", "user"]`. Account name lives in config, not credentials.
- `api_client`: `AppDynamicsApiClientCredentials{client_name, client_secret}`
  — scopes `["system", "user"]`; OAuth token cache + refresh-on-401 lives
  behind this variant only.

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

## Agreed build plan (customer-confirmed, in order)

1. **Mock API first** — `tools/appdynamics/` simulator matching real AppD
   request/response shapes, **verified against the 21.x docs**: fixture JSON
   lifted verbatim from the doc pages' sample payloads, not hand-written.
   (The free-trial route is dead — signup unavailable — so the simulator is
   the primary dev target, and the bank's controller is the only real AppD
   we will ever touch. Consequence: schedule the first live `Test connection`
   against the bank controller EARLY — right after auth works — and treat it
   as the fixture-reconciliation moment, since docs can drift from reality.)
2. **Build the integration** (client + schemas + registry entry).
3. **Auth tests** — username/password with the `@`-append rule (below): both
   `user` + account → `user@customer1`, and a pasted `user@customer1` →
   passed through unchanged (no double-append). Simulator asserts the exact
   Basic-auth decode in both cases; unit tests pin the transform.
4. **End-to-end** via sandbox-feedback-loop against the simulator: create the
   connection through the real UI, index, ask topology/metric questions,
   verify at DB/log/HTTP layers, screenshot.
5. **Logo**: download once from
   `https://connect.redhat.com/s3api/prod-s3api/appdynamics_logo.png` →
   `frontend/public/data_sources_icons/appdynamics.png` (PNG = convention,
   no icon-map edit).

**UI contract (customer mock of the Edit Connection form):** Connection Name;
Controller URL; Account Name; Authentication dropdown defaulting to
"Username + Password" with helper text "Use Username + Password when the
controller cannot issue an API client — common on locked-down on-prem
controllers."; Username with hint "The account name above is appended
automatically unless the username already contains \"@\"."; masked Password;
checkbox **"Skip TLS verification"** ("Use only for self-signed or private-CA
certs on trusted networks (e.g. on-prem controllers). Leave off to verify
certificates.") mapping to `verify_ssl` inverted. All of this derives from the
Pydantic schemas — field `title`/`description` strings carry these labels
verbatim; no frontend code. Note: the checkbox supersedes the earlier
`ca_bundle_path` idea for v1 (matches Splunk/Zabbix); keep `ca_bundle_path`
as a follow-up if the bank's security review requires verification over skip.

**`@`-append rule (the subtle bug surface):** effective Basic-auth login =
`username` if it already contains `@`, else `f"{username}@{account_name}"`.
Edge case: email-style usernames legitimately containing `@` collide with the
heuristic — the rule stays, escape hatch is typing the full `user@account`
form manually.

## Verification strategy (no real controller in CI)

A Zabbix-style testcontainer is **not possible**: there is no official
Controller image, community images require licensed binaries + a `license.lic`,
need many GB of RAM and minutes to boot, and the license cannot be
redistributed. Layered plan instead:

1. **`tools/appdynamics/` simulator** — a small FastAPI stub (~200–300 lines)
   implementing exactly the touched surface: basic-auth check with the
   `user@account` form, the OAuth token endpoint (so the `api_client` variant's
   refresh-on-401 is exercised), the nine REST resources with seeded JSON
   fixtures, `output=JSON` handling, and switchable failure modes (401, 429,
   empty-grant account). Docker-composed like `tools/zabbix/` but boots in
   milliseconds, no license. Precedent: `tools/splunk/wildcard_guard_proxy.py`
   (simulate what can't be containerized). This backs the sandbox-feedback-loop
   and an integration-container entry.
2. **Unit tests with recorded fixtures** (ServiceNow-style) — real controller
   JSON captured once into `tests/unit/fixtures/appdynamics/`, replayed through
   a `_FakeSession`. Always-green layer; guards response-shape fidelity the
   hand-written simulator could drift from.
3. **Remote mode** in `tests/integrations/ds_clients.py` — ServiceNow-style
   (no container), credentials via gitignored `integrations.json`, pointed at a
   real controller when available. Bank lab controller is the acceptance target.

**Fixture source: a free trial account** (SaaS-only, 14 days — don't sign up
until implementation starts, the clock starts at signup). Use it to validate
auth end-to-end and capture fixtures; an empty trial controller has no data, so
run an agent-instrumented sample app against it for an hour first. Caveat: the
trial runs a current (25/26.x) controller while the customer runs 21.4 —
current responses may contain additive fields 21.4 lacks. Treat **21.4's field
set as the contract**: cross-check captured fixtures against the 21.x API
reference, read all fields defensively, and let final validation happen on the
bank's lab controller.

## Open questions for the customer / before implementation

1. ~~Controller version~~ — **answered: on-prem 21.4**; 21.x docs authoritative.
2. Is the Analytics/Events Service licensed and reachable, or Controller-only?
   (Determines whether ADQL is ever in scope.)
3. ~~API Client or basic auth~~ — **answered: username/password**; a dedicated
   read-only service account with Applications & Dashboards Viewer (or
   equivalent custom role) is still needed, plus rotation/lockout policy.
4. Internal CA bundle availability for `ca_bundle_path`.
5. Is Database Visibility licensed? If yes, a `databases` virtual table
   (`/controller/rest/databases` collectors/servers) is a cheap v1.1 addition.

## Scope summary

Read-only, Controller-API-only (target: on-prem 21.4), fixed ten-table
virtual catalog, two auth variants (**username/password default**, API Client
OAuth secondary), `requests`-only, enterprise-gated, no MCP dependency.
Estimated shape and size: very close to `zabbix_client.py` (~450 lines); the
~60 lines of token-lifecycle code apply only to the secondary OAuth variant.

---

## Addendum (2026-08-29): RCA knowledge catalog — dashboards & health rules

Context: the Splunk and Elasticsearch/Kibana connectors now catalog the
deployment's *knowledge objects* (dashboards, saved searches) as
`dashboard::…` / `saved_search::…` tables, so the agent replays the team's
manual-RCA workflow — find the dashboard for the affected service, read what
each panel queries, re-run it scoped to the incident window (see
`splunk_client.py` / `elasticsearch_client.py` and the shared prompt-gate
allowlist in `tables_schema_section.py`). This addendum designs the
AppDynamics equivalent and records a live verification of every open API
question against a real SaaS controller (v26.x trial,
`cloud202608170211412.saas.appdynamics.com`, 2026-08-29).

### What "knowledge" means in AppD

Unlike Splunk/Kibana, an AppD dashboard widget carries no runnable query —
it carries a **metric path + entity scope**, and the connector already
executes metric paths (`metric_data`). And the densest curated knowledge in
AppD is not dashboards but **health rules**: full definitions carry the
metric, the warn/critical thresholds, and the affected entities — "which
metric matters and what *bad* means, numerically". The connector today
surfaces health-rule *violations* but not the rule *definitions*.

### Proposed catalog (mirrors the Splunk/Kibana shape)

- `health_rule::<app>/<name>` — via the **fully supported** Health Rule API:
  `GET /controller/alerting/rest/v1/applications/{id}/health-rules` (list) +
  `/{rule-id}` (definition). Description = condition summary; metadata =
  metric paths + thresholds + `conditionExpression` (e.g.
  `(A AND B) OR (C AND D AND E)`); "execution" = routing the rule's metric
  path through the existing `metric_data` table with the incident window.
  Zero unsupported APIs. **This is the high-value, zero-risk half.**
- `dashboard::<name>` — hybrid:
  - *Listing*: `GET /controller/restui/dashboards/getAllDashboardsByType/false`
    (unsupported `restui`, but see verification below) as best-effort
    auto-discovery, PLUS an optional `dashboard_ids` config list that skips
    the listing entirely (the export API is supported, so the config-list
    path is 100% supported end-to-end).
  - *Hydration* (`get_schema`): `GET /controller/CustomDashboardImportExportServlet?dashboardId=<id>`
    — the **documented** Configuration Import/Export API. Widgets become
    panel columns; each `dataSeriesTemplates[].metricMatchCriteriaTemplate`
    carries `applicationName`, entity scope, and
    `metricExpressionTemplate.inputMetricPath` (note: the export drops the
    redundant `metricPath` key — read `inputMetricPath`).
  - *Execution*: `{"dashboard": ..., "panel": ...}` envelope resolves the
    widget's metric path onto the existing `metric_data` route. No new query
    engine.

### Live verification (real SaaS controller, 2026-08-29)

| Operation | Basic auth (`user@account`) | OAuth API client with NO roles |
|---|---|---|
| OAuth token issuance | n/a | works |
| `rest/applications` | 200 | 200 |
| `restui` dashboard listing | 200, full list | **200, EMPTY list** |
| Export servlet by id | 200, raw dashboard JSON (all widgets) | masked as `Invalid dashboard id` |
| Create (restui) / Import (servlet) | both work | explicit `CREATE_DASHBOARDS` permission error |
| Health rules list + definition | 200 (7 default rules, full `evalCriterias`) | 401 |

Key findings, each a design requirement:

1. **`restui` listing works with plain basic auth AND bearer tokens on
   current SaaS — no session/CSRF dance.** The "restui needs UI-session
   auth" fear is dead on 26.x SaaS; keep the `dashboard_ids` config fallback
   for older on-prem controllers (target 21.4) where this must be re-tested.
2. **Under-privileged identities fail *silently*, not loudly**: empty
   dashboard list and "invalid id" on export — never a 403. The connector
   MUST treat "0 dashboards" as ambiguous and say "no dashboards visible —
   check the API client's role grants / dashboard sharing" in
   `test_connection` output and the catalog description.
3. An API client with no roles can still authenticate and read
   `rest/applications` — role grants gate dashboards and the alerting API
   separately. Setup docs: "assign the API client a viewer role" in bold.
4. The import servlet **silently drops** widget data series whose referenced
   entities (tier/node) don't resolve on the target controller (warnings
   array). Irrelevant to the read-only connector; critical for test seeding —
   fixtures must reference real entities, or be captured from export.
5. Health-rule definitions verified end-to-end on an instrumented app
   (`bow-sample-bank`): 7 default rules, full condition trees with metric
   paths — confirming `health_rule::` catalog entries carry enough to
   re-run the underlying metrics via `metric_data`.

Living test objects left on the trial controller: dashboards `BOW Checkout
Health` (2196), `BOW Checkout Health_1` (2197), `BOW Bank Ops` (2198),
`BOW Bank Ops v2` (2200); apps `bow-sample-bank`, `ACME-Air` (prior
sessions' seeded estates), plus a two-tier `bow-payments` Python-agent
estate (checkout-tier → billing-tier with a billing-error profile) seeded
from this session per the feedback-loop doc's pyagent recipe.

### Verification plan when implemented

Tier 1/2 as usual: extend `tools/appdynamics/mock_controller.py` with the
three endpoints, shaped from the real captured responses; unit fixtures from
the live exports (never hand-written — see finding 4). Tier 2.5: re-run the
probe matrix against the customer's on-prem 21.4 controller (one curl each
for listing/export/health-rules — the only open question left is `restui`
behavior on 21.x). Tier 3: the standard sandbox e2e (alert prompt → agent
finds `dashboard::`/`health_rule::` entries → re-runs metric paths for the
incident window).

### Scope amendment

The v1 exclusion "Dashboards, policies, actions — write/admin surface" was
right for its mission but mischaracterizes the read path: listing + export
is pure read. Amended scope: **v1.1 adds `health_rule::` (supported APIs
only) and `dashboard::` (supported export + best-effort restui listing +
`dashboard_ids` fallback)**; policies/actions/config CRUD stay out.
