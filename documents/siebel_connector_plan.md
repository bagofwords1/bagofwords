# Siebel CRM Connector — Analysis & Implementation Plan

Status: **planning only — no implementation yet**

Question: how do we connect to Oracle Siebel CRM the way we connect to
Salesforce / Oracle DB / SQL Server?

Short answer: **there is no Siebel connector today**, but the connector
architecture (client + config schemas + registry entry + icon) makes it a
contained, Salesforce/ServiceNow-sized addition built on the **Siebel REST
API** — plus two paths that already work *right now* with existing
connectors (direct DB, Oracle BI) documented below.

---

## 1. What we have today

### 1.1 No `siebel` type exists

Nothing in `backend/app/schemas/data_source_registry.py`, no client, no
config schemas, no icon, no open GitHub issue.

### 1.2 Paths that already work without any new code

1. **Direct database access (works today).** Siebel is an on-prem/OCI app
   whose entire data model lives in a regular RDBMS — Oracle, SQL Server,
   or DB2. Our existing `oracledb` / `mssql` connectors can point straight
   at the Siebel schema: accounts in `S_ORG_EXT`, contacts in `S_CONTACT`,
   opportunities in `S_OPTY`, service requests in `S_SRV_REQ`, activities
   in `S_EVT_ACT`, quotes in `S_DOC_QUOTE`, joined through `ROW_ID`/`PAR_ROW_ID`
   conventions. Caveats that make this a workaround, not the answer:
   - the physical schema is huge (~4,000+ tables) with cryptic names and
     heavy extension-column noise (`ATTRIB_01…`, `X_*`) — poor prompt-schema
     material without manual curation (though our `tables`-style config
     filters help);
   - it bypasses Siebel's business layer entirely: no visibility rules,
     no LOV translation, no business-component logic;
   - customers rarely grant read access to the production DB, and Oracle
     support frowns on direct SQL against the Siebel schema.
2. **Oracle BI / Siebel Analytics (works today).** Our `oracle_bi`
   connector speaks the SOAP API whose namespace is literally
   `com.siebel.analytics.web` — OBIEE *is* productized Siebel Analytics.
   Customers running OBIEE/OAS on top of Siebel (very common) can already
   query their curated Siebel subject areas through it.

So the real gap is a **first-class Siebel application-layer connector**,
which is what the rest of this document plans.

## 2. Siebel API surface (what we'd build on)

### 2.1 The Siebel REST API — the right integration point

Available since **Siebel Innovation Pack 2016 (IP16)**, served by the
**Siebel Application Interface** (SAI) in IP17+ (a `siebel-rest.war` on the
embedded Java container in IP16). Anything on 2016+ — which is where
supported installs live; Oracle's continuous-release model (Siebel CRM
18.x → 26.x monthly updates) keeps this API stable at `v1.0`.

Base URI: `https://<host>:<port>/siebel/v1.0/`, four resource families:

- **`/data/{Business Object}/{Business Component}`** — business-layer
  records (Account/Account, Contact/Contact, …). This is the query surface.
  GET query params:
  - `searchspec` — Siebel search spec, e.g.
    `([Location] LIKE 'HQ*' AND [Account Status] = 'Active')`. Field names
    in square brackets, `*` wildcard, `AND`/`OR`, comparison operators,
    `IS NULL`. Same language admins use in list applets, so LLM training
    coverage is decent (comparable to the ServiceNow encoded-query bet).
  - `fields` — comma-separated projection (field names contain spaces:
    `First Name`, `Account Status` — used verbatim).
  - `PageSize` — **default 10, max 100** (server-side `MaximumPageSize` in
    the SAI profile can lower/raise it). Much smaller pages than
    ServiceNow's 1,000 — a 10k-row cap costs up to ~100 requests.
  - `StartRowNum` — 0-based pagination offset.
  - `ViewMode` — visibility filter; documented values `Personal`,
    `Sales Rep`, `Organization`, `Group`, `Catalog` (+ `All` appears in
    Oracle's own examples on some versions). **Default is `Sales Rep`** —
    a major gotcha: an integration user with default ViewMode sees only
    records on its own positions' teams. Must be configurable, and the
    default choice matters (see §3.4).
  - `uniformresponse=y` — forces every response into `{"items": [...]}`
    regardless of record count (otherwise single records come back as a
    bare object). We should always send it.
  - `childlinks` — trim child-link noise from responses.
- **`/data/describe`** (and per-object `/data/{BO}/{BC}/describe`) —
  machine-readable **OpenAPI 2.0/3.0 spec** of every Business Object
  exposed as a Base Integration Object, including per-field definitions.
  This is the schema-discovery endpoint.
- **`/service/{Business Service}`** — RPC-style business service
  invocation. Out of scope (write/workflow oriented).
- **`/workspace/{name}/…`** — repository metadata (dev tooling). Out of
  scope.

### 2.2 Response shape quirks (fixture-worthy)

- Multi-record responses: `{"items": [ {...}, ... ], "Link": [...]}`;
  each item carries a `Link` array (rel=self/parent/child) that must be
  stripped before building a DataFrame.
- **All field values are strings** ("Revenue": "1200.50") — the prompt
  must tell the agent to `pd.to_numeric` before math (ServiceNow has the
  same display-value caveat).
- Field names contain spaces.
- Empty result sets are **not clearly specified**: depending on
  version/config Siebel returns `{"items": []}`, HTTP 204, or **HTTP 404
  with an SBL-DAT error ("There were no matching records")**. The client
  must treat a 404 whose body carries a no-records SBL-DAT message as an
  empty DataFrame, not an error — and this needs verification against a
  real instance.

### 2.3 Authentication

Configured per SAI profile; three modes:

- **Basic auth over TLS** (Base64 user/password per request) — the
  standard integration-user setup, Phase 1 default. Maps to our `userpass`
  variant with `scopes=["system", "user"]` (per-user credentials are free
  via `UserConnectionCredentials`, same as Salesforce/ServiceNow).
- **OAuth 2.0 bearer tokens** — Siebel validates tokens by
  *introspection* against an external OAuth server (it is not its own
  authorization server, unlike ServiceNow). The SAI must be configured in
  SSO mode with an Authentication URL. Because the token issuer is
  customer-specific infrastructure, per-user delegated OAuth is **Phase
  2+, demand-driven** — but the client should accept an `access_token`
  kwarg from day one (cheap, mirrors ServiceNow, keeps the door open).
- **SSO/SAML** — browser-flow oriented, not relevant to a server-side
  connector.

### 2.4 Visibility model (per-user access)

Siebel visibility is genuinely per-user (positions, organizations,
access groups), enforced by the object manager per authenticated session
— same story as ServiceNow ACLs. The graded ladder from the ServiceNow
plan applies verbatim:

1. Phase 1: shared integration user (+ ViewMode config) — the Postgres
   trust model;
2. per-user basic auth via the existing `scopes=["system","user"]`
   machinery — free;
3. per-user OAuth — only if a customer's SAI is already OAuth-fronted.

`catalog_ownership="shared"` is correct: the schema is identical for all
users; visibility filters rows.

### 2.5 What about pre-2016 installs?

Legacy Siebel (8.x/IP15-) has no REST — only SOAP EAI web services or the
Java Data Bean. Both are heavy, per-integration-object, and increasingly
rare as installs consolidate on the continuous-release stream. **Decision:
out of scope.** Legacy customers use the direct-DB path (§1.2) — worth a
line in the connector's docs.

Third-party bridges (CData / Progress JDBC-for-Siebel drivers) exist but
are commercially licensed — not viable as a dependency.

## 3. Design decisions

### 3.1 Query interface for the agent

Siebel's native filter language (searchspec) is scoped to a BO/BC pair, so
— exactly like ServiceNow — a bare string isn't enough; the object must
ride along. **Decision: JSON spec string**, documented in
`system_prompt()`:

```json
{
  "object": "Account",
  "component": "Account",
  "searchspec": "([Account Status] = 'Active' AND [Location] LIKE 'San*')",
  "fields": ["Name", "Location", "Account Status", "Main Phone Number"],
  "limit": 100
}
```

- `component` optional, defaults to `object` (true for all standard CRM
  objects; non-identical pairs like `Action`'s children stay expressible).
- Maps 1:1 to one `/data/{BO}/{BC}` GET; nothing to translate.
- Rejected: SQL→searchspec translation (lossy), raw path passthrough (no
  guardrails).

### 3.2 Schema discovery

**Primary: the `/data/describe` OpenAPI document** — parse each exposed
Business Object's field definitions into our `Table`/`TableColumn` model
(`DTYPE_TEXT/NUMBER/CURRENCY/DATE/DATETIME/BOOL/ID/…` → dtypes). One
request, includes custom `X_*` fields, and only lists objects actually
exposed on that instance.

**Fallback: record sampling.** Describe availability/shape varies by
version and repository configuration (objects must be exposed as Base
Integration Objects). When describe yields nothing usable for an object,
fetch one record (`PageSize=1&uniformresponse=y`) and infer the field
list from its keys — weak types (everything is a string anyway), never
blocked. This mirrors the ServiceNow plan's ACL-fallback thinking.

**Curated default object list** (CRM-centric, mirrors Salesforce's), with
a `objects` config override (comma-separated `BO` or `BO/BC` entries —
slash separator because names contain spaces):

```
Account, Contact, Opportunity, Service Request, Quote, Action
```

(`Action` is Siebel's activities object.) `Id` is always the PK. FKs:
Phase 1 none (describe doesn't expose join targets the way
`sys_dictionary.reference` does); child `Link` rels could seed
parent/child FKs in Phase 2.

### 3.3 Result semantics

- Always send `uniformresponse=y`; read `items`; strip each item's `Link`.
- Paginate `StartRowNum` in `PageSize=100` steps up to the spec's `limit`
  (default 100, hard cap 10,000 — at 100 rows/request that's a deliberate
  100-request worst case; keep the default limit low).
- Treat no-records 404s (SBL-DAT body) as empty results (§2.2).
- Structured errors on 401 (bad credentials / expired token), 403
  (responsibility/visibility), 404 (bad base path or unexposed object —
  actionable message: check SAI URL and that the BO is REST-exposed), 429.
- Read-only: GETs only, `capabilities = {Capability.QUERY}` (base default).

### 3.4 ViewMode default

`Sales Rep` (Siebel's default) silently shows a near-empty dataset for a
typical integration user. Options: default `Organization` (broad,
safe-ish) or `All` (what reporting wants, but needs the right
responsibilities and may not be accepted on all versions as a REST param).
**Recommendation: config field `view_mode`, default `Organization`,
document `All` for admin-blessed reporting users; verify `All` against a
real instance.** `test_connection` should surface the active ViewMode in
its success message so a "why is my data empty" ticket self-diagnoses.

## 4. File-by-file work plan (Phase 1)

Same four-piece pattern as every connector — no bespoke frontend/API work
beyond the icon:

1. **`backend/app/schemas/data_sources/configs.py`**
   - `SiebelCredentials`: `username`, `password` (ui:type password).
   - `SiebelConfig`: `server_url` (e.g. `https://siebel.acme.com:9001`),
     `rest_base` (default `/siebel/v1.0` — SAI deployments sometimes remap
     the context root), `objects` (optional comma-separated override),
     `view_mode` (default `Organization`), `verify_ssl` (default true —
     on-prem SAI often runs self-signed; Zabbix precedent).
2. **`backend/app/schemas/data_source_registry.py`** — entry keyed
   `"siebel"`: `category="services"`, `title="Siebel CRM"`,
   `config_schema=SiebelConfig`,
   `credentials_auth=AuthOptions(default="userpass", by_auth={"userpass":
   AuthVariant(schema=SiebelCredentials, scopes=["system", "user"])})`,
   explicit `client_path="app.data_sources.clients.siebel_client.SiebelClient"`,
   `version="beta"`. Defaults (`data_shape="tables"`,
   `catalog_ownership="shared"`, `ui_form="data_source"`) are correct.
3. **`backend/app/data_sources/clients/siebel_client.py`** (~300 lines,
   `requests`-based — no new dependency; `ServiceNowClient` is the
   template): `connect()` (basic auth or bearer), `_get()` with the
   error/no-records mapping, `test_connection()` (1-record probe of the
   first configured object + ViewMode in the message),
   `get_schemas()`/`get_schema()` (describe → sampling fallback),
   `execute_query()` (JSON spec → paginated GETs → DataFrame),
   `prompt_schema()`, `system_prompt()` (searchspec syntax guide,
   spaces-in-field-names and everything-is-a-string caveats),
   `description`. Constructor kwargs must match config+credentials field
   names (`construct_client` merges both dicts and narrows to the
   signature).
4. **`frontend/public/data_sources_icons/siebel.png`** — convention
   fallback (`DataSourceIcon.vue`) picks it up with no code change;
   missing icon degrades to the generic document icon.
5. **`backend/tests/unit/test_siebel_client.py`** — FakeSession at the
   `requests.Session` boundary, `test_servicenow_client.py` style:
   auth header selection, connection test, describe→schema parsing,
   sampling fallback, spec parsing/rejection, pagination/limit-cap,
   `Link`-stripping, no-records-404 → empty DataFrame, error mapping.
6. **`CHANGELOG.md`** — feature entry.

Nothing needed in the agent layer — it works generically off any client.

## 5. Test & dev environment

Unlike ServiceNow, **there is no free Siebel developer instance** — no
PDI-equivalent, no official Docker image; Siebel installs are licensed
on-prem/OCI deployments. Consequences:

- Unit tests carry the design: hand-built fixtures encoding the documented
  quirks (`items`+`Link` shape, spaced field names, string values,
  uniformresponse behavior, no-records 404).
- The uncertain behaviors (empty-result status code, `ViewMode=All`
  acceptance, describe response shape on the target version) must be
  validated against a customer/partner instance during beta — hence
  `version="beta"` and the sampling fallback, which keeps the connector
  functional even where describe surprises us.
- Optional: a small FastAPI mock (`tests/mocks/` precedent) emulating
  `/siebel/v1.0/data/*` for e2e coverage.

## 6. Phasing

- **Phase 1 (MVP)** — §4: basic auth (+`access_token` kwarg), curated
  objects + describe/sampling discovery, searchspec JSON-spec queries,
  tests, icon, registry at `version="beta"`.
- **Phase 2** — child-link FKs; LOV enrichment for the prompt schema;
  OAuth bearer wiring for SAI-with-OAuth deployments; promote to `1.0.0`.
- **Phase 3 (demand-driven)** — per-user delegated OAuth; business-service
  invocation; attachment retrieval.

## 7. Open questions

1. **Which Siebel version(s) do the requesting customers run?** Pre-2016
   → REST doesn't exist; steer to the direct-DB path instead of building
   a SOAP EAI connector.
2. **ViewMode default** — `Organization` vs `All` (§3.4); needs a real
   instance to verify `All` acceptance per version.
3. **Empty-result contract** — `{"items": []}` vs 204 vs no-records 404
   (§2.2); handle all three defensively, confirm in beta.
4. **Describe coverage** — are the curated CRM objects exposed as Base
   Integration Objects out-of-the-box on the target instance, or does the
   customer's repo config limit `/data/describe`? Determines how often the
   sampling fallback actually runs.
