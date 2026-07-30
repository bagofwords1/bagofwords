# Priority ERP Connector — Research & Recommendation

**Status:** Research / analysis only — no implementation.
**Goal:** Let users connect their Priority ERP (Priority Software) tenant to BOW so the
agent can query and act on ERP data.
**Hard requirement:** must support **on-premise** Priority, not just Priority Cloud.
**Researched:** 2026-07-25. Every endpoint claim below was live-probed or read from
Priority's own developer portal; see §7 for the probe method.

---

## 0. Bottom line up front

1. **There is no official Priority MCP server, hosted or self-hostable.** Priority
   Software's developer portal (`prioritysoftware.github.io`) documents exactly six
   integration surfaces — Web SDK, **REST API (OData)**, Priority SDK, Webhooks, ODBC,
   OIDC Authentication — and mentions MCP nowhere. The official MCP registry
   (`registry.modelcontextprotocol.io`) returns **zero** Priority Software entries. The
   V26.0 "AI-first ERP" launch (2026-05-20) ships an embedded *aiERP Companion* and
   in-product agents — Priority consuming AI internally, not Priority exposing an MCP
   endpoint outward. So "add Priority MCP" cannot mean "add a preset pointing at
   Priority's MCP server." That server does not exist.

2. **What does exist is a clean, well-documented OData v4 API** —
   `https://{host}/odata/Priority/{tabula}.ini/{company}` — with `$metadata` for full
   schema discovery, `$filter/$select/$expand/$orderby/$top/$skip/$since`, subforms as
   navigation properties, batch writes, and three auth modes (Basic, PAT, OAuth2 PKCE).
   This is a *better* integration substrate than most vendors' MCP servers, because
   `$metadata` gives us the whole schema instead of a fixed tool list.

3. **Recommendation: build a native `priority_erp` connector, modeled directly on the
   existing ServiceNow connector.** Priority maps onto `ServiceNowClient` almost
   field-for-field (§4). This gives catalog indexing, table-shaped data, per-user auth,
   and the agent's normal query path — rather than a bag of opaque tools.

4. **Do not route this through the third-party MCP bridges.** Zapier / viaSocket /
   Workato all expose Priority, but as a narrow, fixed set of *write* actions (create
   sales order, create lead, …) behind a per-account endpoint and a metered
   subscription. That is an automation surface, not an analytics surface — it cannot
   answer "what were Q3 sales by customer."

5. **One gap worth flagging early:** Priority's service root is **per-tenant** (host,
   `tabula.ini` file, and company name all vary per customer). Both `McpPreset.server_url`
   and `CustomApiPreset.base_url` are *fixed strings*, so neither preset mechanism can
   express Priority as a one-click tile. A native connector sidesteps this entirely —
   `instance_url` is already a per-connection config field on ServiceNow. (Same gap
   blocks a dbt Cloud preset, for the same reason.)

6. **On-prem is well served — and the auth story is the opposite of what you'd expect.**
   The OData API ships *with* the on-prem application server (auto-installed, hosted
   under IIS), so the protocol surface is identical on-prem and in cloud. But OAuth2 is
   **documented for on-prem only** — Priority states the OAuth2 guide "is relevant only
   for on-prem (non-SaaS) installations." So per-user delegated auth is available
   *on-prem* (External ID module) and falls back to bring-your-own-PAT in cloud — which
   is Priority's own recommendation for non-browser clients. See §2, §2b and §6c
   (§6c also states precisely how confident the cloud finding is).

---

## 1. Does Priority have an MCP server? — the evidence

| Source | Finding |
|---|---|
| `prioritysoftware.github.io` (official dev portal) | Sections: Before you Begin, Web SDK, **REST API**, Priority SDK, Webhooks, ODBC, OIDC Authentication. **No MCP, no agent API.** |
| Official MCP registry (`registry.modelcontextprotocol.io/v0/servers?search=priority`) | 0 Priority Software results (only unrelated `task-priority-guidance-*` entries). |
| `mcp.priority-software.com`, `mcp.priority-connect.online` | DNS does not resolve. |
| `https://t.eu.priority-connect.online/mcp` (demo tenant) | HTTP 403 from **CloudFront WAF** — a blanket edge block, *not* an MCP endpoint. The same 403 is returned for `/.well-known/*`, while `/odata/...` correctly returns 401. Not evidence of an MCP server. |
| Priority V26.0 launch (2026-05-20) | aiERP Companion + autonomous agents **inside** the ERP. No outbound MCP endpoint announced. |

**Conclusion: nothing official to point a preset at.** Anything called a "Priority MCP
server" today is community-built or a generic OData bridge.

---

## 2. Priority's actual API surface (what we'd build on)

### Service root — per tenant
```
https://{server}/odata/Priority/{tabula}.ini/{company}
```
- `{server}` — customer's Priority application server (cloud or on-prem)
- `{tabula}.ini` — the tabula.ini in use (e.g. `tabbtd38.ini` on the demo tenant)
- `{company}` — the company's internal Priority name (e.g. `usdemo`)

Documented demo root: `https://t.eu.priority-connect.online/odata/Priority/tabbtd38.ini/usdemo`
(verified live: returns **401** without credentials, i.e. it is up and auth-gated).

### Schema discovery — and where column titles actually come from

- `GET {root}/$metadata` → EDMX XML: every entity type, property, and navigation
  property (subforms).
- `GET {root}/GetMetadataFor(entity='ORDERS')` → single-entity metadata (v25.0+).
  Exists **because** full-system metadata is slow to generate (§6b).

**The plain EDMX carries no human labels.** Priority's own sample:

```xml
<EntityType Name="ABILITIES">
  <PropertyRef Name="ABILITYCODE"/>
  <Property Name="ABILITYCODE" Type="Edm.String" MaxLength="3"/>
  <Property Name="ABILITYDES"  Type="Edm.String" MaxLength="32"/>
  <NavigationProperty Name="ABILITYVALUES_SUBFORM"
                      Type="Collection(Priority.OData.ABILITYVALUES)" ContainsTarget="true"/>
</EntityType>
```

Name, type, length, keys, subforms — **no titles**. Titles arrive separately, as
**annotations**:

| Annotation | Meaning | Since |
|---|---|---|
| `Priority.OData.Description` | The column **title** — e.g. `String="Customer Number"` for `CUSTNAME` | — |
| `Priority.OData.Mandatory` | `Bool="true"` on required columns | v25.1 |

So the "column titles become business vocabulary" plan **does hold** — but it depends on
`Priority.OData.Description`, not on the `<Property>` elements. A parser that reads only
`<Property Name= Type=>` would produce a catalog of `CUSTNAME`/`ORDNAME` with no labels at
all. **This is the single easiest way to build a useless catalog.**

**Titles are localized to the environment language.** Priority's own docs show
`Priority.OData.Description` values in Hebrew (`String="לקוחות"`). An Israeli tenant
returns Hebrew titles — good for matching user vocabulary, but the catalog is not
guaranteed English, and any curated English form list must key off `Name`, never title.

### ⚠️ Custom fields need a metadata rebuild *inside Priority*

> *"Whenever private customizations add fields to a form, these new fields will not appear
> in the REST API until the **REST metadata is rebuilt** for that form."*

A customer's bespoke fields (the four-letter-prefix ones, §3 A3) are invisible to us until
someone rebuilds REST metadata in Priority. When an admin says "my custom field is
missing," this — not our indexer — is the first thing to check. Worth surfacing in the
connector's help text.

### Query capabilities
`$filter`, `$select`, `$expand`, `$orderby`, `$top`, `$skip`, plus Priority's own
**`$since`** (v20.0+) for incremental pulls — exactly what a catalog indexer wants.

Subforms expand as navigation properties, including nested:
```
GET {root}/ORDERS?$filter=CUSTNAME eq '1011'
    &$expand=ORDERITEMS_SUBFORM($filter=PRICE gt 3;$select=PARTNAME,PRICE)
```
Note: with composite keys, `$select` must include all parent key fields.

### Response shape
Standard OData JSON — `@odata.context` + `value[]`, typed as `Edm.String`,
`Edm.Int64`, `Edm.Decimal`, `Edm.DateTimeOffset`.

### Authentication — three modes, and deployment decides which you get

| Mode | How | On-prem | Cloud | BOW scope |
|---|---|---|---|---|
| **Basic** | API-licensed Priority user — a dedicated API username from the admin, set in the Personnel File, **not** the normal login. Cannot be used while External ID access is enabled. | ✅ | ✅ | `system`, `user` |
| **PAT** (v19.1+) | `Authorization: Basic base64(<PAT>:PAT)` — the token is the *username*, the password is the literal string `PAT`. Managed in *System Management → System Maintenance → Users → REST Interface Access Tokens*. Multiple PATs per user, independently revocable. Recommended for server-to-server. | ✅ | ✅ | `system`, `user` |
| **OAuth2 / External ID** | **Authorization Code + PKCE only** (no other grant types). Client auth at the token endpoint is **HTTP Basic**. Scope `openid rest_api`. Requires the paid **External ID module**, and all Priority users must sign into the Priority UI via an external IdP. | ✅ | ❌ | `user` (delegated) |

> **The correction that matters:** Priority's own docs scope the OAuth2 guide with
> *"The following guide is relevant only for on-prem (non-SaaS) installations."*
> OAuth2 is documented as **on-prem only**; treat it as unavailable in cloud unless
> Priority confirms otherwise (§6c weighs the evidence). Cloud gets Basic and PAT.
>
> Two further preconditions, even on-prem: the paid **External ID module**, *and* all
> Priority users already signing into the Priority UI through an **external IdP**.

**Consequence for per-user auth.** Per-user identity is what makes Priority's own
permission model fire — it applies "any relevant permission restrictions" per
authenticated user, and a shared service account collapses every identity into one
(same argument as the SAP/ServiceNow connectors). Two different mechanisms get us there:

- **On-prem:** true delegated OAuth (`oauth` variant, `scopes=["user"]`).
- **Cloud:** no OAuth exists, so per-user means **bring-your-own PAT** — each user pastes
  their own token. This is already a supported pattern in the codebase: `zabbix` does
  exactly this with `token: AuthVariant(..., scopes=["system", "user"])`.

### OAuth2 endpoints — derived per tenant, not constants

`PRIORITY_DOMAIN` is the OData service URL with everything from `/odata` onward removed
(e.g. service root `https://priority.acme.local/odata/Priority/tabula.ini/acme`
→ domain `https://priority.acme.local`):

| | |
|---|---|
| Discovery | `https://{PRIORITY_DOMAIN}/accounts/.well-known/openid-configuration` |
| Authorize | `https://{PRIORITY_DOMAIN}/accounts/connect/authorize` |
| Token | `https://{PRIORITY_DOMAIN}/accounts/connect/token` |

This is the **ServiceNow pattern, not the PowerBI pattern** — endpoints are derived from
a config field at request time, exactly as `connection_oauth_service.py:194-230` already
does with `{instance_url}/oauth_auth.do`. No new OAuth machinery is needed: PKCE
(`generate_pkce_pair`, same file) and `client_secret_basic` are both already implemented
and in use.

### Rate limits — **Priority Cloud only**
- **100 API calls/minute per user**
- **15 concurrent requests** (10 processed, 5 queued)
- **3-minute** per-request timeout
- Over-limit → **HTTP 429**

A catalog indexer walking `$metadata` across hundreds of forms will hit this immediately
in cloud. On-prem has no such documented ceiling — throughput is whatever the customer's
IIS/app server sustains. So the limiter must be **configurable, not hardcoded**: default
to the cloud numbers, let on-prem raise or disable them.

### Licensing gates (both deployments)
- The **API module** must be purchased, plus an **API license per user**.
- Basic auth needs a user with an API license.
- **Writes need "transaction packages"** — another paid unit. Read-only avoids this
  entirely, which is a further argument for shipping read-only first.

---

## 2b. On-premise specifics

On-prem is a **hard requirement**, and it's well served — but it differs from cloud in
five concrete ways:

| | On-prem | Cloud |
|---|---|---|
| **OData API** | Auto-installed with the Priority application server; hosted under the app-server website in **IIS**; requires the **ASP.NET Core Hosting Bundle** on that server | Managed by Priority |
| **Service root** | Customer's own host — internal DNS or IP | `*.priority-connect.online` and similar |
| **OAuth2** | ✅ available (External ID module) | ❌ not available |
| **Rate limits** | None documented; bounded by the customer's IIS | 100/min, 15 concurrent, 429 |
| **TLS** | Frequently self-signed / internal CA | Public CA |

Design implications:

1. **`verify_ssl: bool = True` is required on the config schema.** Internal Priority
   hosts routinely use self-signed certs. The codebase convention is well established —
   ~15 connectors already carry this field with the "disable only for self-signed certs
   on internal hosts" wording.
2. **Don't assume HTTPS or a public hostname.** Accept a full service-root URL rather
   than composing one from a cloud-shaped template.
3. **Reachability is a deployment concern, not a code concern.** There is no tunnel or
   edge-agent concept in this codebase; on-prem connectors (`powerbi_report_server`,
   `qlik_sense`, `infor_olap`, `sap_hana`) simply take a URL and assume the BOW instance
   can route to it. Since BOW ships as docker-compose/k8s, an on-prem customer runs it
   inside their own network. Worth stating explicitly to whoever scopes this.
4. **Version skew is worse on-prem.** Cloud tenants are current; on-prem customers sit on
   whatever they last upgraded to. Feature gates in §5 (`$since` v20.0+, PAT v19.1+,
   `GetMetadataFor` v25.0+, ODBC v22.1.21+) are real branching conditions on-prem.
5. **On-prem is where per-user auth is actually achievable**, since OAuth2 exists there.
   Somewhat counterintuitively, the on-prem story is the *stronger* one.

---

## 3. The options, compared

### Option A — Native `priority_erp` connector ✅ recommended
A `PriorityErpClient` in `backend/app/data_sources/clients/`, registered with
`data_shape="tables"`, reading `$metadata` for schema and issuing OData queries.

- **Pro:** forms become *tables* → they flow into the existing catalog/schema/agent
  query path, not an opaque tool list. `$metadata` means schema is discovered, never
  hand-maintained. Per-tenant service root is just config. Per-user OAuth is already a
  solved pattern in this codebase. `$since` gives cheap incremental re-indexing.
- **Con:** most upfront work; needs its own rate limiter.
- **Effort:** comparable to `ServiceNowClient` (458 lines) — the structure ports almost
  directly (§4).

### Option B — `custom_api` preset
Pre-fill an OData base URL + a curated endpoint list as callable tools.

- **Pro:** cheapest; machinery already exists (`CustomApiPreset`, `CUSTOM_API_PRESETS`).
- **Con, and it's fatal for the preset framing:** `CustomApiPreset.base_url` is a fixed
  string but Priority's root is per-tenant, so the tile can only ever be a hint the admin
  must overwrite. Endpoints would be hand-curated instead of discovered. Data arrives as
  tool output, not tables — no catalog, no schema-aware querying.
- **Also blocking:** `custom_api` offers `none | bearer | api_key | oauth_app` — **no
  Basic auth variant**, so Priority PAT (`Basic base64(PAT:PAT)`) can't be expressed
  without adding one. Stuffing credentials into `CustomAPIConfig.headers` is not an
  option: that field is plaintext config, not secrets.
- **Verdict:** viable only as a stopgap for a *single* customer, and only after adding
  a basic-auth variant.

### Option C — self-hosted OData→MCP bridge, connected as a generic MCP server
Point BOW's existing `mcp` connector at a bridge that auto-generates MCP tools from
Priority's `$metadata`. Candidates:

| Bridge | Runtime | Notes |
|---|---|---|
| [`OData/MCP`](https://github.com/OData/MCP) | .NET 8 / ASP.NET Core middleware | From the OData org itself. Auto-generates tools from metadata; can be hosted at `/mcp`. **"PREVIEW 1 COMING SOON"** — pre-release. MIT. |
| [`oisee/odata_mcp_go`](https://github.com/oisee/odata_mcp_go) | Go, single binary | Most deployable. OData v2 + v4, reads `$metadata` at startup, generates CRUD + action tools. Built for SAP, generic by design. |
| Python / .NET ports of the same | — | Same design, other runtimes. |

- **Pro:** no BOW code; auto-discovers every entity.
- **Con:** we'd operate the bridge (deploy, patch, secure). Tools are generic CRUD, so
  the agent sees `filter_ORDERS`-style tools rather than tables. And the credential
  problem moves into the bridge — it authenticates to Priority with *one* identity,
  destroying per-user permissions unless the bridge forwards identity itself.
- **Verdict:** a good way to *prototype* the tool surface in a day. Not a shipping answer.

### Option A2 — ODBC / direct SQL to the Priority database ❌ (the obvious on-prem instinct)
On-prem raises the natural question: Priority runs on MSSQL or Oracle, and BOW already
has `mssql` and `oracledb` clients — why not point one at the ERP database directly?

Priority's own ODBC driver rules this out, and so does its architecture:

- **It is not the database.** "The driver does not provide direct access to the
  underlying database (MSSQL or Oracle), but rather to a wrapper which presents the same
  data available in Priority forms via the UI." Each form is a table.
- **64-bit Windows only.** BOW's backend is Linux — the driver cannot be loaded at all.
- **Read-only** (SELECT only), **no subqueries**, text forms and picture fields
  inaccessible.
- **Row cap of `MAXFORMLINES`** — default **10,000** rows per result.
- **Field-level privileges and data authorization are not implemented.** For a connector
  whose whole point is honoring Priority's permission model, this is disqualifying on its
  own.
- Going *around* the driver to the raw MSSQL/Oracle schema means reverse-engineering
  Priority's internal tables with no supported contract, no permission enforcement, and
  breakage on every upgrade.

**Verdict: no.** OData is the supported, permission-respecting, platform-neutral surface
on-prem *and* in cloud — and it's the same API in both. One client covers both
deployments.

### Option A3 — hybrid: Priority's dictionary as the semantic layer, SQL as the engine ⚠️ on-prem only

Option A2 rejects the *ODBC driver*. It does not settle the sharper question: on-prem you
can reach the MSSQL/Oracle database directly — what exactly does the form layer add that
raw SQL loses, and is it recoverable?

**What the form layer actually adds** (from Priority's SDK docs):

| Form-layer feature | Lost by direct SQL? | Recoverable? |
|---|---|---|
| **Calculated columns** — "values determined by other columns; their values are **not stored in any table**" | **Yes, entirely** | Only by re-implementing the formula |
| **Form = base table + join tables** — "each form is derived from a single base table"; "can display data from several different tables… any number of join tables" | Yes — you must reconstruct the joins | ✅ from the dictionary |
| **Column `title` vs `name`** — name is what SQL uses, title is the human label | Yes — SQL gives cryptic names, no labels | ✅ from the dictionary |
| **Triggers / business logic** — "prevents users from circumventing business rules through direct database queries" | Yes | ❌ — but only matters for **writes** |
| **Data authorization / field-level privileges** | **Yes, completely** | ❌ **not recoverable** |
| **Per-company scoping** — application tables "maintain data separately for each Priority company" | Yes — queries must scope by company | ✅ mechanical |

**The important nuance: most of the semantic layer is *data*, not behavior.** Form
definitions, base/join tables, column name↔title mappings, relations and calculated-column
formulas all live in Priority's **Form Generator system tables**. So the dictionary is
readable — via SQL *or* OData — and can drive the catalog independently of how queries
execute. The semantic layer is not lost; it has to be *read* rather than inherited.

**What direct SQL buys — and it is not marginal.** Priority's documented OData query
options are `$filter`, `$select`, `$expand`, `$orderby`, `$top`, `$skip`, `$since`.
**`$apply` is absent** — there is no documented aggregation or `groupby` support. For a
BI product that is severe: "revenue by customer by month" cannot be pushed down at all.
Every aggregate means paginating raw rows out of the ERP and aggregating client-side,
against a 100 req/min cloud ceiling. Direct SQL gives real `JOIN` / `GROUP BY` / window
functions, no rate limit, and orders-of-magnitude better analytical performance — and
BOW already ships `mssql` and `oracledb` clients.

**The shape that follows:** dictionary-driven catalog + SQL execution + OData for writes —
the same split `sap-connector-analysis.md` argues for (SQL first, OData second) and that
`powerbi_report_server` already implements (metadata over REST, data over DuckDB/Parquet).

**Two constraints decide whether this is allowed:**

1. **Permissions.** Direct SQL bypasses Priority's data authorization *unconditionally* —
   no hybrid recovers it. Acceptable only under a shared analyst/service-account model.
   If per-user enforcement is required, **OData is the only conforming path**, and the
   aggregation cost has to be absorbed.
2. **Deployment.** Direct DB access is realistic **on-prem only**; Priority Cloud does
   not expose MSSQL/Oracle. So SQL-first would give on-prem tenants a materially better
   product than cloud tenants, and means maintaining two execution paths.

**Verdict:** genuinely attractive on-prem, and worth costing — but it is a *second*
execution backend, not a substitute for the OData client. Ship OData first (it is the
only path that works everywhere and the only one that preserves permissions), then add
SQL execution as an on-prem performance mode behind the same dictionary-driven catalog.
See §5 Q7 — confirming whether `$apply` truly is unsupported is the single
highest-value experiment available on a real tenant, because it decides how badly the
OData-only path hurts.

### Option A4 — the SDKs ⚠️ two different things, neither is the base integration

"The Priority SDK" is ambiguous — the developer portal ships **two** unrelated things,
and they land in completely different places.

#### A4a. Priority SDK — in-product customization, *not* a client library
An **internal development environment** for building custom forms, procedures, reports
and triggers **inside** Priority. It's SQL-based, the code runs in the ERP, and it's
installed into a customer's Priority installation. You cannot "use it" to connect from
BOW — there is nothing to import.

**But it is the one clean answer to the aggregation gap (§3 A3).** Because SDK code runs
server-side inside Priority, you can write a custom aggregating report/procedure, expose
it as a normal form, and read it over OData. That gets you server-side `GROUP BY` **while
keeping permissions, calculated columns and the form layer intact** — precisely what
direct SQL sacrifices.

The cost is deployment, and it's not small: custom objects must be installed **per
customer** (each with its own mandatory four-letter prefix), maintained across Priority
upgrades, and built by someone with Priority development skills. That turns "connect your
Priority" into "install our ERP extension."

**Verdict:** a per-customer accelerator for a big tenant with heavy aggregation needs —
not a product-wide integration. Worth remembering the moment §5 Q7 confirms `$apply` is
unsupported.

#### A4b. Web SDK (`priority-web-sdk`) — a real client library, wrong shape for reads
| | |
|---|---|
| Package | `priority-web-sdk`, npm, **ISC** |
| Version | **3.0.39, published 2026-07-06** — actively maintained (first published 2017) |
| Runtime | JavaScript, **isomorphic** — `require('xhr2')` + `typeof window` guards confirm it runs under Node, not browser-only |
| Deps | `base-64`, `moment-timezone`, `xhr2` — small |

**Why it's not the data path:**

1. **Wrong language.** BOW's backend is Python. Using it means running and operating a
   Node sidecar — real cost, for no read benefit.
2. **Stateful and row-oriented, not a query API.** The model is
   `login()` → open form → `getRows()` → `setActiveRow()`. That's a *form session*. For
   bulk analytical reads it is chattier and slower than OData, not better.
3. **It does not solve aggregation either.** `getRows` is still row-by-row. The `$apply`
   gap is untouched — arguably worsened by the extra round-trips.
4. **It duplicates OData for reads**, at higher operational cost.

**Where it genuinely wins — actions, not analytics.** The Web SDK can run Priority
**procedures** (multi-step business processes), print/send documents such as invoices and
order confirmations, and trigger form actions and calculated fields. **OData cannot run
procedures at all.** If the agent should eventually *do* things in Priority — kick off a
process, email an invoice — this is the correct surface.

**Verdict:** not the base integration, and not a substitute for the OData client. Keep it
in reserve for the writes/actions phase (§6 step 4), where its procedure support is
something no other option offers.

### Option D — third-party automation MCP (Zapier / viaSocket / Workato) ❌
Zapier's Priority MCP exposes a fixed action set: create potential customer, create
sales order, create opportunity, create lead, update order status, add shipping charges,
plus a few lookup triggers.

- **Con:** write-automation shaped, not analytics shaped. Per-account generated
  endpoint (not a shareable preset URL), metered subscription, and a third party sits
  between BOW and the customer's ERP data. No `$metadata`, no arbitrary queries.
- **Verdict:** no. Doesn't serve a BI product.

### Option E — community `priority-mcp` (aviranbenmoshe) ❌ as a dependency
A Claude Desktop-oriented server: **stdio** transport, configured via `PRIORITY_URL` /
`PRIORITY_USER` / `PRIORITY_PASS`, auto-discovering forms from Priority metadata, with
CRUD + subform support, a 100 calls/min limiter, and audit logging.

- **Con:** stdio — BOW's `McpClient` speaks `streamable_http`/`sse` to *remote* servers,
  so it can't consume this as-is. Single shared credential from env vars. Unaffiliated
  with Priority Software.
- **Verdict:** don't depend on it — but **do read it**. Its form auto-discovery and its
  100/min limiter are direct evidence of the design constraints in §2, and it's the
  closest prior art to Option A.

---

## 3b. It's a regular connector — PowerBI's shape, ServiceNow's OAuth wiring

Nothing here needs new framework capability. The two halves are both existing patterns:

**Structure it like `powerbi`** — a per-tenant SaaS/on-prem system with catalog
discovery and two auth variants, one service-level and one per-user:

```python
"powerbi": DataSourceRegistryEntry(
    credentials_auth=AuthOptions(default="service_principal", by_auth={
        "service_principal": AuthVariant(..., scopes=["system"]),
        "oauth":             AuthVariant(..., schema=OAuthDelegatedCredentials, scopes=["user"]),
    }),
    catalog_nouns=("model table", "model tables"),
)
```

**Wire the OAuth branch like `servicenow`**, not like PowerBI. PowerBI's endpoints are
Microsoft-global constants; Priority's are per-tenant, derived from config — which is
precisely what the ServiceNow branch at `connection_oauth_service.py:194-230` already
does. A Priority branch is the same handful of lines, deriving `PRIORITY_DOMAIN` from the
service root instead of reading `instance_url`.

Already implemented, nothing to add:

| Priority needs | Already in the codebase |
|---|---|
| Authorization Code + **PKCE only** | `generate_pkce_pair()`, `connection_oauth_service.py:46-55` |
| Token endpoint via **HTTP Basic** | `token_endpoint_auth_method="client_secret_basic"` (used by X) |
| Public client (no secret) | `client_secret=None` → PKCE-only, supported |
| Per-tenant authorize/token URLs | ServiceNow branch, same file |
| `pat` / `basic` auth-variant keys | Both already used elsewhere in the registry |
| Self-signed TLS on internal hosts | `verify_ssl` field convention, ~15 connectors |
| Bring-your-own-token per user | `zabbix` `token` variant, `scopes=["system","user"]` |

**Icon:** drop the brand mark at
`frontend/public/data_sources_icons/priority_erp.png`
(source: `https://www.priority-software.com/wp-content/uploads/2023/06/fav.png`, verified
reachable — 2,626-byte PNG). `DataSourceIcon.vue` resolves
`/data_sources_icons/{type}.png` by convention, so no frontend code change is needed —
the file just has to be named after the connector type.

---

## 4. Why ServiceNow is the right template

`ServiceNowClient` already solves the same problem shape — a per-tenant SaaS business
system with a REST API, a metadata catalog, and per-user delegated OAuth:

| ServiceNow | Priority ERP equivalent |
|---|---|
| `instance_url` (`https://acme.service-now.com`) | Service root (`https://{host}/odata/Priority/{tabula}.ini/{company}`) |
| Table API `/api/now/table/{table}` | OData entity set `/{FORM}` |
| `sys_dictionary` + table hierarchy for schema | `$metadata` (EDMX) — standard and richer |
| `tables` config (curated list) | Curated ERP forms: `ORDERS`, `ORDERITEMS`, `CUSTOMERS`, `PART`, `AINVOICES`, `PORDERS`, `LOGPART`, … |
| `discover_all` (incl. `u_`/`x_` custom tables) | Discover all forms from `$metadata` (incl. customer-specific forms) |
| `sysparm_query` | `$filter` / `$orderby` / `$top` / `$skip` |
| `display_values` | Priority returns display values natively |
| `userpass` + per-user `oauth` variants | `pat` / `basic` + per-user `oauth` (External ID) |
| — | **`$since`** for incremental indexing (bonus) |
| — | **Rate limiting: 100/min, 15 concurrent, 429** (new work) |

Registry entry would follow `"servicenow"` closely:

```python
"priority_erp": DataSourceRegistryEntry(
    type="priority_erp",
    category="services",
    title="Priority ERP",
    description="Priority Software ERP (cloud and on-premise) — orders, customers, parts, "
                "invoices and custom forms via the OData REST API.",
    # service_root, verify_ssl, forms, discover_all, rate-limit overrides
    config_schema=PriorityErpConfig,
    credentials_auth=AuthOptions(default="pat", by_auth={
        # PAT: works cloud + on-prem. `user` scope = bring-your-own token,
        # which is the ONLY per-user path in Priority Cloud (no OAuth there).
        "pat":   AuthVariant(title="Personal Access Token", schema=..., scopes=["system", "user"]),
        # Basic: API-licensed user. Unavailable while External ID is enabled.
        "basic": AuthVariant(title="Username / Password",   schema=..., scopes=["system", "user"]),
        # Delegated OAuth: ON-PREM ONLY (External ID module). Endpoints derived
        # per tenant from PRIORITY_DOMAIN, ServiceNow-style.
        "oauth": AuthVariant(title="Sign in with Priority", schema=OAuthDelegatedCredentials, scopes=["user"]),
    }),
    client_path="app.data_sources.clients.priority_erp_client.PriorityErpClient",
    version="beta",
),
```

The `oauth` variant should be surfaced conditionally or clearly labelled "on-premise
only" — offering it to a cloud tenant produces a sign-in that cannot succeed.

---

## 5. Open questions for the team

1. **Which customer/tenant is driving this, and on what version?** Version gates are
   real branching conditions, and worse on-prem where upgrades lag: `$since` needs
   v20.0+, PAT needs v19.1+, `GetMetadataFor` needs v25.0+, mandatory-field annotations
   need v25.1+.
2. **On-prem: is the External ID module licensed?** It's the only route to per-user
   OAuth. Without it, on-prem falls back to one shared API account (or per-user PATs)
   and Priority's per-user permission model doesn't fire.
3. **Is the API module + per-user API licenses in place?** No API license, no API access
   — worth confirming before any build starts.
4. **How does BOW reach the on-prem host?** BOW instance inside the customer network, or
   is a documented network path expected? There is no tunnel/agent concept today.
5. **Read-only or read/write?** Read-only more than halves the scope *and* avoids the
   paid "transaction packages" gate. Writes need Priority's batch/composite-key
   semantics and BOW's `confirm: true` policy path.
6. **Which forms matter?** A curated starter set beats indexing every form on a tenant,
   given the cloud 100/min ceiling and the sheer size of a full `$metadata` document.
7. **Does Priority's OData support `$apply` (aggregation)?** Not documented — their query
   page lists only `$filter/$select/$expand/$orderby/$top/$skip/$since`. **Verify on a
   real tenant before committing to an OData-only design.** If aggregation genuinely
   can't be pushed down, every analytical question pulls raw rows through a 100 req/min
   ceiling, and the on-prem SQL execution path in §3 Option A3 stops being an
   optimization and becomes close to a requirement.
8. **Is a shared service-account model acceptable, or is per-user enforcement required?**
   This single answer decides Option A3: direct SQL bypasses Priority's data
   authorization unconditionally, so per-user enforcement forces OData-only.

---

## 6. Suggested sequencing

1. **Prototype (≈1 day):** run `oisee/odata_mcp_go` against a Priority tenant, connect
   it to BOW as a generic bearer-auth MCP server, and see what the agent actually does
   with ERP tools. Cheap way to validate the tool surface before committing to Option A.
2. **Ship (Option A), read-only, PAT auth:** `PriorityErpClient` + registry entry +
   config/credential schemas + icon. One client serves **cloud and on-prem** — same
   OData API, differing only by service root and `verify_ssl`. Curated form list,
   `$metadata`-driven schema, configurable token-bucket limiter with 429 backoff
   (cloud defaults, relaxable on-prem).
3. **Then, on-prem per-user auth:** the `oauth` variant against External ID, deriving
   `PRIORITY_DOMAIN` from the service root. Reuses the ServiceNow branch wholesale.
4. **Then:** `discover_all` from `$metadata`, `$since` incremental indexing, and finally
   gated writes (blocked on transaction-package licensing). If writes should include
   *running Priority business processes* rather than just row CRUD, the Web SDK's
   procedure support (§3 A4b) is the only surface that offers it — at the cost of a Node
   sidecar.

---

## 6b. The build spec — system auth + per-user auth + full object indexing

The three requirements (per-user auth, system auth, PowerBI-style indexing of all objects
with metadata) are satisfied by **one pattern the codebase already runs**: the
ServiceNow/BigQuery dual-mode credential. One connection carries *both* a service
credential that drives catalog indexing and an OAuth client that powers per-user sign-in.

### Auth: one credential blob, both modes

```python
class PriorityErpCredentials(BaseModel):
    """Service PAT drives catalog indexing and system-scope queries; the oauth_*
    fields enable per-user "Sign in with Priority" (on-prem / External ID only).
    Mirrors ServiceNowCredentials / BigQueryCredentials."""
    pat: str                                    # token as username, password literal "PAT"
    oauth_client_id: Optional[str] = None       # per-user sign-in (on-prem only)
    oauth_client_secret: Optional[str] = None
```

`construct_client` already strips `oauth_`-prefixed keys before they reach the client, so
the client only ever sees the service credential.

| Requirement | Mechanism |
|---|---|
| **System auth** | `pat` (or `basic`) variant, `scopes=["system"]` → `auth_policy="system_only"`. Drives **catalog indexing** and shared queries. |
| **Per-user auth, on-prem** | `oauth` variant, `scopes=["user"]` → `auth_policy="user_required"`. True delegated OAuth via External ID; Priority applies each user's own permissions. |
| **Per-user auth, cloud** | `pat` variant with `scopes=["system","user"]` → each user supplies **their own PAT**. The `zabbix` bring-your-own-token pattern. |

> **The one asymmetry to accept up front:** Priority Cloud has no OAuth (§2), so "per-user"
> there means bring-your-own-PAT rather than delegated sign-in. Both enforce per-user
> permissions inside Priority — the difference is enrollment UX, not security. On-prem
> gets the better flow.

**Indexing keeps working under `user_required`.** That is exactly why the service
credential and the OAuth client live in the same blob: the catalog is crawled with the
system PAT while queries execute as the signed-in user. Without it, a `user_required`
connection has nothing to index with (see `connection_indexing_service.py:534` — warming
runs with no `current_user`).

### Indexing: the PowerBI shape, but far cheaper

Registry placement:

```python
data_shape="tables",
catalog_ownership="shared",              # the form dictionary is org-wide…
catalog_nouns=("form", "forms"),         # …Priority catalogs *forms*, not DB tables
```

`catalog_ownership="shared"`, **not** `per_user`: every user sees the same forms and
fields: only *row visibility* differs, and that is enforced at query time by whichever
auth mode is active. Modelling it `per_user` would force a full re-crawl per user for no
gain.

**One Priority form → one BOW `Table`:**

| `Table` field | Source |
|---|---|
| `name` | OData entity set (form name) |
| `description` | Form title |
| `columns[].name` | OData property name (what queries use) |
| `columns[].description` | **Form column *title*** — the human label. This is the highest-value field for agent comprehension: it turns cryptic column names into business vocabulary. |
| `columns[].dtype` | EDMX type (`Edm.String`, `Edm.Decimal`, `Edm.DateTimeOffset`…) |
| `columns[].metadata` | `{mandatory, calculated, choice_values}` — `Priority.OData.Mandatory` is annotated in v25.1+ |
| `pks` | EDMX `<Key>` (handles Priority's composite keys) |
| `fks` | Navigation properties — subforms and related forms, giving the agent join paths |
| `metadata_json` | `{"priority": {"form", "base_table", "join_tables", "company", "subforms"}}` |

**Cost: one call, but a slow one.** An earlier draft claimed this was "essentially one
HTTP call — dramatically cheaper than PowerBI." That overstated it. Priority warns
explicitly:

> *"With the large number of Priority entities, generating and downloading the metadata
> file for the entire system **can take a while**."*

That warning is precisely why `GetMetadataFor(entity='X')` exists. So: a full crawl is
*one request* rather than PowerBI's per-dataset fan-out (so it barely touches the
100 req/min ceiling), but it is a **long, heavy** request — plan for a generous timeout
(Priority's own per-request ceiling is 3 minutes) and expect first-index latency.
Incremental refresh via `GetMetadataFor` matters more than first assumed.

**Incremental refresh** mirrors PowerBI's `prior_tables` parameter: forms already present
are rebuilt from the stored definition; only new/changed ones are re-read, via
`GetMetadataFor(entity='X')` (v25.0+) rather than re-pulling the full EDMX.

### Deliverables

| # | File | What |
|---|---|---|
| 1 | `configs.py` | `PriorityErpConfig` (service_root, `verify_ssl`, forms, `discover_all`, rate-limit overrides) + `PriorityErpCredentials` (+ basic variant) |
| 2 | `data_source_registry.py` | `"priority_erp"` entry — 3 auth variants, `data_shape="tables"`, `catalog_ownership="shared"`, `catalog_nouns=("form","forms")` |
| 3 | `clients/priority_erp_client.py` | `test_connection`, `get_schemas(force_refresh, prior_tables)` from `$metadata`, `get_schema`, `execute_query` (OData), token-bucket limiter + 429 backoff, `verify_ssl` |
| 4 | `connection_oauth_service.py` | `priority_erp` branch deriving `PRIORITY_DOMAIN` from the service root → `/accounts/connect/{authorize,token}`, scope `openid rest_api`, `client_secret_basic`, PKCE. Modelled on the ServiceNow branch at :194-230 |
| 5 | `data_sources_icons/priority_erp.png` | Brand icon — resolves by convention, no frontend change |
| 6 | `tests/` | `$metadata` → `Table` parsing (fixture EDMX), auth-header construction for all three modes, rate-limiter/429 behaviour |

Scope for v1: **read-only**. Writes need transaction-package licensing and the
`confirm: true` policy path — separate change.

---

## 6c. Auth verification, part 1 — against Priority's published docs

Verified against `prioritysoftware.github.io/restapi/authenticate/`, quoted verbatim.

| Claim | Priority's own words | Verdict |
|---|---|---|
| PAT header format | *"the Authorization header should be set to Basic, the username should be the PAT that was defined, and the password should be **hardcoded to `PAT`**"* | ✅ exact |
| PAT since v19.1 | *"Introduced in version 19.1"*; *"Multiple PATs can be associated with a single Priority user… replaced or deleted independently"* | ✅ |
| Basic = separate API user | *"defined in the **API User Name** field of the Personnel File form and is **separate from the user's standard user name**"* | ✅ |
| Basic ⊥ External ID | *"You **cannot** use Basic Authentication (method 1) while External ID access is enabled."* | ✅ |
| **OAuth2 is on-prem only** | *"The following guide is relevant **only for on-prem (non-SaaS) installations**. It was tested using Priority v22.1."* | ✅ |
| **PKCE only** | *"**Only** the Authorization code (with PKCE) flow is supported."* | ✅ |
| S256 | *"Code Challenge Method: **SHA-256**"* | ✅ matches `generate_pkce_pair` |
| **Basic client auth** | *"Client Authentication: **Send as Basic Auth header**"* | ✅ → `client_secret_basic` |
| Scope | *"Scope: `openid rest_api`"* | ✅ |
| Endpoints | `…/accounts/connect/authorize`, `…/accounts/connect/token`, discovery `…/accounts/.well-known/openid-configuration` | ✅ |
| `PRIORITY_DOMAIN` | *"whatever comes before the 'odata' segment of the URL"* | ✅ derive from `service_root` |

### Three things the docs added that the plan didn't have

**1. Priority's docs independently prescribe our dual-mode design.** Verbatim:

> *"OAuth2 is intended for scenarios where end-users are interacting with your application
> using a browser or mobile app. To authenticate **automated server-to-server
> communications** where no browser or mobile app is involved, use the **Personal Access
> Token (PAT)** method instead."*

That is exactly §6b: PAT for system/catalog indexing (server-to-server, no browser),
OAuth for per-user queries (browser flow). The architecture is Priority's own recommendation,
not an inference.

**2. Client registration is a manual admin step in the Priority UI** — this was missing
from the setup story. The admin registers BOW under
*System Management → System Maintenance → Users → Manage IDs Externally → **External
Applications***. Priority then **auto-generates the Application ID and Secret ID**, and the
admin adds BOW's **Redirect URL** in a subform. Directly analogous to ServiceNow's
*System OAuth → Application Registry*, and it must be documented in the connect form's help
text or admins will not find it.

**3. Priority is a confidential client — the secret is required, not optional.**
The registration always issues a Secret ID, and client auth is Basic. So
`oauth_client_secret` must be **mandatory** when the `oauth` variant is chosen —
`_apply_client_auth` raises `ValueError("client_secret_basic requires a client_secret")`
otherwise. §6b listed it as `Optional`; that is right for the *schema* (it's unused for
`pat`/`basic`) but the connect form must enforce it for `oauth`.

**Precondition worth surfacing early:** OAuth2 requires that *"all Priority users
authenticate with the Priority UI using an external IdP."* A customer without SSO
configured cannot use per-user OAuth **even on-prem, even with the module purchased**.

### Cloud OAuth — how confident, exactly

Earlier this doc asserted flatly that Priority Cloud has no OAuth. Tightening that to what
is actually supported:

- **Documentation (strong):** the OAuth2 guide is scoped *"only for on-prem (non-SaaS)"*,
  and its prerequisites are on-prem concepts ("The Priority Application server is
  configured and running").
- **Live probe of the cloud demo tenant (suggestive, not conclusive):**
  `/accounts/.well-known/openid-configuration` → **404** (application-level), while
  `/odata/…` on the same host → **401**, proving the host is reachable. But
  `/accounts/connect/{authorize,token}` → **403**, and this tenant is behind a CloudFront
  WAF that returns 403 for unrelated paths too — so those are not clean evidence.

**Treat cloud OAuth as unavailable unless Priority confirms otherwise.** Either way the
design is unaffected: cloud per-user runs on bring-your-own-PAT, which is documented,
supported, and Priority's own recommendation for non-browser clients.

---

## 6d. Auth verification, part 2 — against our code

Not assumed. Each requirement below was checked against the actual implementation.

| Priority requires | Codebase provides | Verdict |
|---|---|---|
| **PKCE only** (no other grant types) | `routes/connection_oauth.py:207` generates a PKCE pair **unconditionally** for every provider, sends `code_challenge_method=S256` (:229), stores the verifier in a cookie (:255), and passes it to the exchange (:334) | ✅ |
| **Token endpoint via HTTP Basic** | `_apply_client_auth` (`connection_oauth_service.py:299-338`): `client_secret_basic` returns `httpx.BasicAuth` **and** pops `client_id`/`client_secret` out of the body — exactly what Priority's IdP expects | ✅ |
| Basic client auth **on refresh too** | `refresh_access_token` (:395-407) calls the same `_apply_client_auth` | ✅ |
| **Per-tenant** authorize/token URLs | The ServiceNow branch (:194-230) already derives endpoints from a **config** field (`instance_url`) rather than constants. Priority derives `PRIORITY_DOMAIN` from `service_root` identically | ✅ |
| Scope `openid rest_api` | `oauth_params["scopes"]` is a free-form string per branch | ✅ |
| **Bearer token reaches the client** | `resolve_credentials` returns `access_token` in the creds dict → `construct_client` narrows to the constructor signature (`connection_service.py:1318-1333`) → client sets `Authorization: Bearer` when present, else Basic. `ServiceNowClient.__init__` (:104-143) is the exact template | ✅ |
| `oauth_*` keys must not leak to the client | `construct_client:1316` strips every `oauth_`-prefixed key | ✅ |

### The requirement that actually needed verifying: indexing under `user_required`

`connection_service.py:1351-1361`, verbatim:

> *"System/indexing path (no user in context): fall back to the saved admin/system
> credentials so the initial catalog can be built. This only runs for admin-side
> operations (schema/tool indexing, warm-up) that always pass `current_user=None` —
> per-user runtime queries pass a real user and resolve their own credentials below."*

**Confirmed:** system PAT indexes the catalog, per-user token executes queries, on the
same connection. Requirements 1 and 2 are satisfied by existing behaviour.

### ⚠️ One flag that must be set, or per-user silently degrades

`supports_user_token()` (`connection_identity.py:34-37`) gates the entire delegated path
on:

```python
return "oauth" in (connection.allowed_user_auth_modes or [])
```

If `allowed_user_auth_modes` doesn't contain `"oauth"`, `resolve_credentials` **skips the
delegated branch entirely** and falls through to the legacy path — no error, just the
wrong identity. This must be set on the connection. It is the single easiest thing to get
wrong in this design.

### Cloud vs on-prem, mechanically

- **On-prem** → `allowed_user_auth_modes=["oauth"]` → delegated branch → token refresh via
  `maybe_refresh_oauth_credentials`.
- **Cloud** → no OAuth exists, so `supports_user_token` is `False` and per-user PATs flow
  through the **legacy `user_required` path** (:1412+, "non-delegated user_required
  connections"), which reads the user's own `row.decrypt_credentials()`. Different code
  path, same guarantee: queries run under the user's own Priority identity.

---

## 6e. Indexing all objects, and getting them into per-table context

### Correcting §6e's first draft: what actually renders

An earlier version of this section claimed provider metadata reaches the model only via
`TableFormatter` / `ServiceFormatter` branches. **That was wrong**, and the tableau/OLAP
question is what exposed it. The real path:

**The agent's table context is built by `tables_schema_section._render_topk_tables_full`
(:499-580), which emits its own XML and uses neither formatter.** The formatters feed
`client.prompt_schema()`, a secondary/legacy path (`data_source_service:4257-4273`).

What that context path renders **generically, for every connector, with no branch**:

| Rendered automatically | Source |
|---|---|
| `<table name= description= score= usage= instructions= cols=>` | `Table.name` / `.description` |
| `<column name= dtype= description= role=>` | `TableColumn.name/.dtype/.description`, and `metadata["kind"] or metadata["role"]` |
| `<pks>`, `<fks>` | `Table.pks` / `.fks` |

What needs a **per-provider branch** — table-level `metadata_json` only:

| Branch | Covers |
|---|---|
| `type == "semantic_view"` (:538) | Snowflake semantic views |
| `powerbi_report_server` (:562-576) | PBIRS |
| `_render_powerbi_cloud_metadata_xml` (:68) | Power BI cloud |

### So: yes, tableau/OLAP have the same gap — and it's pre-existing

Answering the question directly. `tableau`, `oracle_bi`, `businessobjects`, `qlik_sense`,
`sisense` and `xmla_base` (SAP BW / Analysis Services) all populate namespaced table-level
`metadata_json` — `{"tableau": {...}}`, `{"oracle_bi": {"subjectArea", …}}`,
`{"businessobjects": {"universe_id", "universe_name", "folder"}}` — and **none of it is
rendered in the agent's table context.** Only PowerBI (both flavours) and semantic views
have branches. `ServiceFormatter` has a `tableau` branch, but that only feeds the
secondary `prompt_schema()` path.

**This is an existing gap in the product, not something Priority introduces.** Worth its
own ticket: a generic `metadata_json` renderer would fix Tableau, Oracle BI,
BusinessObjects, Qlik, Sisense and SAP BW at once, and Priority would then need no branch
at all.

### What this means for Priority — much less work than §6b implied

**Column-level metadata — the highest-value part — needs zero *render* code.** Verified
by executing `_render_topk_tables_full` against a Priority-shaped `Table`:

```xml
<table name="ORDERS" cols="3" description="Customer Orders">
<columns>
<column name="ORDNAME"  dtype="Edm.String"  description="Order Number"/>
<column name="CUSTNAME" dtype="Edm.String"  description="Customer"/>
<column name="TOTPRICE" dtype="Edm.Decimal" description="Total Price" role="calculated"/>
</columns>
<pks><pk name="ORDNAME" dtype="Edm.String"/></pks>
<fks><fk column="CUSTNAME" ref_table="CUSTOMERS" ref_column="CUSTNAME"/></fks>
</table>
```

`description` and `role="calculated"` render with no branch; `metadata_json={"priority":…}`
is **absent from that output**, confirming table-level metadata needs one.

**But the client must extract titles from `Priority.OData.Description` annotations**, not
from `<Property>` elements (§2). Zero render code ≠ zero parser work: get the annotation
parsing wrong and every `description=` above is empty.

**Only the table-level block needs a branch** — form name, base table, join tables,
subforms, company:

```xml
<table name="ORDERS" description="Customer Orders" cols="24">
  <columns>
    <column name="ORDNAME"  dtype="Edm.String"  description="Order Number"/>
    <column name="CUSTNAME" dtype="Edm.String"  description="Customer"/>
    <column name="TOTPRICE" dtype="Edm.Decimal" description="Total Price" role="calculated"/>
  </columns>
  <pks><pk name="ORDNAME"/></pks>
  <fks><fk column="CUSTNAME" ref_table="CUSTOMERS" ref_column="CUSTNAME"/></fks>
  <priority base_table="ORDERS" join_tables="CUSTOMERS,AGENTS" company="usdemo"
            subforms="ORDERITEMS_SUBFORM,ORDISTATUSLOG_SUBFORM">
    Calculated columns are not stored and CANNOT be used in $filter or $orderby.
  </priority>
</table>
```

That `<priority>` note is the highest-value line in the block: it prevents the agent from
filtering on calculated columns, which would fail at runtime every time.

### Which formatter, for the secondary path

Priority is API-shaped, not SQL — so `prompt_schema()` should return
**`ServiceFormatter`**, matching `servicenow`, `salesforce`, `tableau`, `oracle_bi` and
`powerbi`. §6b's deliverable 7 said `TableFormatter`; that was wrong. Note
`ServiceFormatter` renders only `column: {name} type: {dtype}` — it drops column
descriptions entirely — so this path is strictly weaker than the main context path. Fine,
since it is not what feeds the agent.

### "Take all" — safe, and cheap

- `discover_all=True` by default; parse the **whole `$metadata`** — still a single HTTP
  request, so indexing every form on a tenant costs about the same as indexing ten.
- **Context won't blow up.** `render_combined(top_k_per_ds=10, index_limit=200)` gives
  full schema to only ~10 tables per source and a compact index for up to 200. A tenant
  with thousands of forms is a catalog-size question, not a context-size one.
- **But cold start needs help.** Top-K selection is driven by score/usage, and a fresh
  connection has neither. Ship a curated default-active set of the common ERP forms
  (`ORDERS`, `ORDERITEMS`, `CUSTOMERS`, `PART`, `AINVOICES`, `PORDERS`, `LOGPART`) as
  `is_active` defaults — **not** as an indexing filter. Everything stays indexed and
  reachable; the curated set just wins the first few turns until real usage data exists.

### Revised deliverables (7, was 6)

| # | File | What |
|---|---|---|
| 1-6 | *(as §6b)* | config/creds, registry entry, client, OAuth branch, icon, tests |
| **7** | `ai/context/sections/tables_schema_section.py` | `_render_priority_metadata_xml` → `<priority base_table= join_tables= company= subforms=>` + the calculated-column warning, inside `<table>`. **The only render change actually required.** |
| *(none)* | — | Column titles → `TableColumn.description` and calculated → `metadata["role"]` render generically. No formatter change needed. |
| *(optional)* | `ai/prompt_formatters.py` | `ServiceFormatter` branch for the secondary `prompt_schema()` path. Low value — that path doesn't feed agent context. |

Plus: registry entry must set `allowed_user_auth_modes` to include `"oauth"` (§6d), and
the connect form must require `oauth_client_secret` for the `oauth` variant (§6c).

---

## 7. Method note

DCR/OAuth capability claims were produced by live-probing each candidate with the same
discovery chain the backend uses (`mcp_dcr_service.discover_mcp_oauth`: RFC 9728
protected-resource metadata → RFC 8414 AS metadata → `registration_endpoint`), plus an
unauthenticated `initialize` POST to confirm the endpoint speaks MCP. Priority's OData
root, `$metadata` behaviour, and auth modes come from Priority's own developer portal
(`prioritysoftware.github.io/restapi/`, updated Dec 2025).

## 8. Sources

- [Priority REST API docs](https://prioritysoftware.github.io/restapi/) ·
  [Authentication](https://prioritysoftware.github.io/restapi/authenticate/) ·
  [Query options](https://prioritysoftware.github.io/restapi/query/) ·
  [Request/response & `$metadata`](https://prioritysoftware.github.io/restapi/request/)
- [Priority ODBC driver](https://prioritysoftware.github.io/odbc) — form wrapper, read-only, 64-bit Windows only
- [Priority OData API PDF](https://cdn.priority-software.com/docs/Priority_OData_API.pdf)
- [Priority developer portal](https://prioritysoftware.github.io/)
- [Priority Software on odata.org](https://www.odata.org/ecosystem/producers/Priority-Software/) — on-prem API install / IIS notes
- [Priority V26.0 AI-first ERP announcement](https://www.priority-software.com/blog/news/priority-software-unveils-prioritys-ai-first-erp-powered/)
- [Official MCP registry](https://registry.modelcontextprotocol.io/) — no Priority entries
- [`OData/MCP`](https://github.com/OData/MCP) · [`oisee/odata_mcp_go`](https://github.com/oisee/odata_mcp_go)
- [Priority SDK](https://prioritysoftware.github.io/sdk/Introduction) (in-product customization) · [Forms & tables model](https://prioritysoftware.github.io/sdk/Forms)
- [Priority Web SDK](https://prioritysoftware.github.io/api/) · [`priority-web-sdk` on npm](https://www.npmjs.com/package/priority-web-sdk)
- [Community Priority MCP server](https://lobehub.com/mcp/aviranbenmoshe-priority-mcp)
- [Zapier Priority MCP](https://zapier.com/mcp/priority) · [viaSocket Priority MCP](https://viasocket.com/mcp/priority)
