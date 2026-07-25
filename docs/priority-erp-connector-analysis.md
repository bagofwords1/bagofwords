# Priority ERP Connector — Research & Recommendation

**Status:** Research / analysis only — no implementation.
**Goal:** Let users connect their Priority ERP (Priority Software) tenant to BOW so the
agent can query and act on ERP data.
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

### Schema discovery
- `GET {root}/$metadata` → EDMX XML: every entity type, property, and navigation
  property (subforms).
- `GET {root}/GetMetadataFor(entity='ORDERS')` → single-entity metadata (v25.0+),
  which matters because the full `$metadata` for a Priority tenant is very large.
- Mandatory fields annotated `Priority.OData.Mandatory` (v25.1+) — useful for
  generating write tools that validate before calling.

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

### Authentication — three modes

| Mode | How | Fits BOW scope |
|---|---|---|
| **Basic** | API-licensed Priority user (distinct from the login user, set in Personnel File). Unavailable while External ID is enabled. | `system` |
| **PAT** (v19.1+) | `Authorization: Basic base64(<PAT>:PAT)` — the token is the username, password is the literal string `PAT`. Managed in *System Management → System Maintenance → Users → REST Interface Access Tokens*. Multiple PATs per user, independently revocable. | `system`, `user` |
| **OAuth2 / External ID** | **Authorization Code + PKCE only.** Discovery at `https://{domain}/accounts/.well-known/openid-configuration`; authorize `…/accounts/connect/authorize`; token `…/accounts/connect/token`; scope `openid rest_api`; token endpoint uses **Basic** client auth. Requires purchasing the **External ID module**. | `user` (per-user delegated) |

Per-user OAuth is the mode that makes Priority's own permission model fire — Priority
applies "any relevant permission restrictions" per authenticated user. Same argument as
the SAP/ServiceNow connectors: a shared service account collapses every identity into one.

### Rate limits (Priority Cloud) — must be designed for
- **100 API calls/minute per user**
- **15 concurrent requests** (10 processed, 5 queued)
- **3-minute** per-request timeout
- Over-limit → **HTTP 429**

A catalog indexer walking `$metadata` across hundreds of forms will hit this
immediately. Needs a token-bucket limiter + 429 backoff, unlike ServiceNow.

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
    description="Priority Software ERP — orders, customers, parts, invoices via the OData REST API.",
    config_schema=PriorityErpConfig,          # service_root/host+tabula+company, forms, discover_all
    credentials_auth=AuthOptions(default="pat", by_auth={
        "pat":   AuthVariant(title="Personal Access Token", schema=..., scopes=["system", "user"]),
        "basic": AuthVariant(title="Username / Password",   schema=..., scopes=["system", "user"]),
        "oauth": AuthVariant(title="Sign in with Priority", schema=OAuthDelegatedCredentials, scopes=["user"]),
    }),
    client_path="app.data_sources.clients.priority_erp_client.PriorityErpClient",
    version="beta",
),
```

---

## 5. Open questions for the team

1. **Which customer/tenant is driving this?** Their Priority version gates real
   features: `$since` needs v20.0+, PAT needs v19.1+, `GetMetadataFor` needs v25.0+,
   mandatory-field annotations need v25.1+.
2. **Is the External ID module licensed?** Without it there is no OAuth2, so per-user
   permissions are impossible and everything runs through one API account.
3. **Cloud or on-prem?** On-prem may sit behind a VPN and won't have the cloud rate
   limits.
4. **Read-only or read/write?** Read-only more than halves the scope. Writes need
   Priority's batch/composite-key semantics and BOW's `confirm: true` policy path.
5. **Which forms matter?** A curated starter set beats indexing every form on a tenant
   given the 100/min ceiling.

---

## 6. Suggested sequencing

1. **Prototype (≈1 day):** run `oisee/odata_mcp_go` against a Priority tenant, connect
   it to BOW as a generic bearer-auth MCP server, and see what the agent actually does
   with ERP tools. Cheap way to validate the tool surface before committing to Option A.
2. **Ship (Option A):** `PriorityErpClient` + registry entry + config/credential
   schemas, read-only, PAT auth, curated form list, `$metadata`-driven schema, token-bucket
   rate limiter with 429 backoff.
3. **Then:** per-user OAuth (External ID), `discover_all` from `$metadata`, `$since`
   incremental indexing, and finally gated writes.

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
- [Priority OData API PDF](https://cdn.priority-software.com/docs/Priority_OData_API.pdf)
- [Priority developer portal](https://prioritysoftware.github.io/)
- [Priority V26.0 AI-first ERP announcement](https://www.priority-software.com/blog/news/priority-software-unveils-prioritys-ai-first-erp-powered/)
- [Official MCP registry](https://registry.modelcontextprotocol.io/) — no Priority entries
- [`OData/MCP`](https://github.com/OData/MCP) · [`oisee/odata_mcp_go`](https://github.com/oisee/odata_mcp_go)
- [Community Priority MCP server](https://lobehub.com/mcp/aviranbenmoshe-priority-mcp)
- [Zapier Priority MCP](https://zapier.com/mcp/priority) · [viaSocket Priority MCP](https://viasocket.com/mcp/priority)
