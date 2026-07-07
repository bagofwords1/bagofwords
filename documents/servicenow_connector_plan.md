# ServiceNow Connector — Implementation Plan

Status: **planning only — no implementation yet**

Goal: add a first-class ServiceNow data source connector, in the same family as
PostgreSQL / Salesforce — admin connects an instance, the platform indexes its
schema, and the agent queries it at runtime.

---

## 1. How connectors work today (context)

A connector in bagofwords is four pieces, all registry-driven:

| Piece | Location | What it does |
|---|---|---|
| Client | `backend/app/data_sources/clients/<type>_client.py` | Subclass of `DataSourceClient` (`base.py`): `test_connection`, `get_schemas`, `get_schema`, `execute_query`, `prompt_schema`, `description` |
| Config / credentials schemas | `backend/app/schemas/data_sources/configs.py` | Pydantic models with `ui:type` extras — the frontend renders forms from these automatically |
| Registry entry | `backend/app/schemas/data_source_registry.py` | `DataSourceRegistryEntry` keyed by type; declares title, auth variants, `client_path`, `data_shape`, `ui_form` |
| Icon | `frontend/public/data_sources_icons/<type>.png` | `DataSourceIcon.vue` falls back to `/data_sources_icons/{type}.png` by convention |

Because the admin form is generated from the Pydantic schemas and the client is
resolved via `resolve_client_class()`, **no bespoke frontend or API work is
needed** beyond the icon.

Closest reference implementations:
- `salesforce_client.py` — service connector with its own query language (SOQL), curated object list, schema via describe API.
- `posthog_client.py` — pure REST connector using `requests`, predefined schemas, structured error handling on `test_connection`. This is the best template for ServiceNow.

## 2. ServiceNow API surface (what we'll build on)

ServiceNow has no SQL endpoint. The relevant REST APIs:

- **Table API** — `GET /api/now/table/{table}` with:
  - `sysparm_query` — "encoded query" language (e.g. `active=true^priority=1^ORDERBYDESCsys_created_on`)
  - `sysparm_fields`, `sysparm_limit`, `sysparm_offset`, `sysparm_display_value`
- **Aggregate API** — `GET /api/now/stats/{table}` for COUNT/SUM/AVG/MIN/MAX with `sysparm_group_by`. (Phase 2)
- **Schema discovery** — query the Table API against ServiceNow's own metadata tables:
  - `sys_db_object` — list of tables (thousands; we will NOT enumerate all)
  - `sys_dictionary` — per-table field metadata: name, `internal_type`, and `reference` (the target table of reference fields → natural foreign keys for our `Table.fks`)
  - `sys_choice` — choice-list values (Phase 2, enriches the prompt schema)

**Auth options ServiceNow supports:** Basic auth (username/password), OAuth 2.0
(several grants), and API key (`x-sn-apikey`, newer releases).

**Dependencies:** none new. Use `requests` (already pinned in
`backend/pyproject.toml`), same as PostHog. The `pysnow` library is
unmaintained — plain REST is cleaner and matches the codebase style.

## 3. Design decisions

### 3.1 Query interface for the agent

The agent calls `client.execute_query(query)`. Salesforce passes a SOQL string,
PostHog a HogQL string. ServiceNow's native "language" is the encoded query,
which is table-scoped, so a bare string isn't enough — the table must ride
along.

**Decision: accept a JSON string spec**, documented in `system_prompt()`:

```json
{
  "table": "incident",
  "query": "active=true^priority=1^ORDERBYDESCsys_created_on",
  "fields": ["number", "short_description", "priority", "state", "assigned_to"],
  "limit": 100
}
```

- Maps 1:1 to a Table API call; nothing to parse or translate.
- Encoded query syntax is heavily represented in LLM training data (it's what
  ServiceNow list filters copy to clipboard), so generation quality is good.
- `system_prompt()` documents the operators (`^` AND, `^OR`, `LIKE`,
  `ORDERBY`/`ORDERBYDESC`, `javascript:gs.daysAgo(n)` date helpers, dot-walking
  through reference fields e.g. `assigned_to.name`).
- Phase 2 adds optional `group_by` / `aggregations` keys routed to the
  Aggregate API.

Rejected alternatives: SQL→encoded-query translation (complex, lossy); raw
URL/params passthrough (no guardrails, worse prompting story).

### 3.2 Schema discovery

Enumerating all tables is a non-starter (a vanilla instance has 3,000+). 

**Decision: curated default table set + config override.**

Default set (ITSM-centric, mirrors Salesforce's curated object list):

```
incident, change_request, problem, task,
sc_request, sc_req_item, sc_task,
sys_user, sys_user_group, cmdb_ci, kb_knowledge
```

- `ServiceNowConfig.tables` — optional comma-separated override/extension, same
  pattern as ClickHouse's `database` config field.
- `get_schema(table)` queries `sys_dictionary` for that table: field name +
  `internal_type` → `TableColumn`; `reference` → populate `Table.fks` pointing
  at the referenced table (real FK info — better than most service connectors).
- `sys_id` is always the PK.
- Note: `sys_dictionary` rows for a table don't include fields inherited from
  parent tables (e.g. `incident` extends `task`). Query with
  `sysparm_query=name=task^ORname=incident` style resolution of the table
  hierarchy via `sys_db_object.super_class`, or use `/api/now/doc/table/schema`
  (Table Schema API) where available. Implementation detail to settle in Phase 1;
  the hierarchy walk via `sys_db_object` is the safe default since it needs no
  extra roles.

### 3.3 Auth

**Phase 1: Basic auth** (username/password of a service account with `rest_api`
/ read roles). This is `SalesforceCredentials`-level effort and covers the
majority of real deployments, which provision a dedicated integration user.

**Phase 2: OAuth 2.0.** The registry's `AuthOptions.by_auth` supports multiple
variants per type, so this is additive:
- `oauth_app` variant (client_id + client_secret + refresh token) for system
  scope.
- Possibly per-user delegated OAuth (`OAuthDelegatedCredentials`, like
  BigQuery/Power BI have) — larger scope because it touches the shared OAuth
  delegated infra; only if a customer needs per-user ServiceNow ACLs.

Optional cheap addition in phase 1 or 2: API-key variant (single `api_key`
field) since it's just a header swap.

### 3.4 Result semantics

- **Pagination**: loop `sysparm_offset` in pages of 1,000 (server default cap)
  up to the request's `limit` (default 100, hard cap ~10,000 rows) so a
  runaway query can't OOM the worker.
- **Display values**: `sysparm_display_value=true` by default — reference
  fields and choices come back human-readable ("Hardware" instead of a
  `sys_id`), which is what the agent and end user want. Exposed as a
  `ServiceNowConfig.display_values` boolean for admins who need raw values.
- **Errors**: mirror PostHog's `test_connection` — distinguish 401 (bad
  credentials), 403 (missing role/ACL), 404 (bad instance URL / table),
  429 (rate limited), with actionable messages.
- **Read-only**: the client only issues GETs. `capabilities = {Capability.QUERY}`
  (the base default). No write-back.

## 4. File-by-file work plan (Phase 1)

1. **`backend/app/schemas/data_sources/configs.py`**
   ```python
   class ServiceNowCredentials(BaseModel):
       username: str   # ui:type string
       password: str   # ui:type password

   class ServiceNowConfig(BaseModel):
       instance_url: str          # e.g. https://acme.service-now.com
       tables: Optional[str]      # comma-separated extra/override tables
       display_values: bool = True
   ```

2. **`backend/app/schemas/data_source_registry.py`**
   - Import the two schemas.
   - Add entry:
     ```python
     "servicenow": DataSourceRegistryEntry(
         type="servicenow",
         title="ServiceNow",
         description="Cloud platform for IT service management, operations, and workflows.",
         config_schema=ServiceNowConfig,
         credentials_auth=AuthOptions(default="userpass", by_auth={
             "userpass": AuthVariant(title="Username / Password",
                                     schema=ServiceNowCredentials,
                                     scopes=["system", "user"]),
         }),
         client_path="app.data_sources.clients.servicenow_client.ServiceNowClient",
         version="beta",
     ),
     ```
   - `client_path` must be **explicit**: dynamic resolution would derive
     `ServicenowClient` (lowercase n) from the type name, and the registry
     comments already flag the convention fallback as a bug source.
   - Defaults are correct as-is: `data_shape="tables"`,
     `catalog_ownership="shared"`, `ui_form="data_source"`.

3. **`backend/app/data_sources/clients/servicenow_client.py`** (~300–400 lines)
   - `ServiceNowClient(DataSourceClient)` with
     `__init__(self, instance_url, username, password, tables=None, display_values=True)`
     — constructor kwargs must match config+credentials field names, since
     `Connection.get_client()` merges both dicts into constructor params.
   - `connect()` contextmanager yielding a `requests.Session` with basic auth
     (PostHog pattern).
   - `test_connection()` — `GET /api/now/table/sys_user?sysparm_limit=1`,
     structured success/failure messages per §3.4.
   - `get_schemas()` / `get_schema(table)` — sys_dictionary + hierarchy walk,
     reference fields → fks.
   - `execute_query(query)` — parse JSON spec, call Table API, paginate,
     return `pd.DataFrame`.
   - `prompt_schema()` — `ServiceFormatter(self.get_schemas()).table_str`.
   - `system_prompt()` / `description` — encoded-query syntax guide with
     examples (the Salesforce client shows the shape; ServiceNow's needs to be
     meatier because encoded queries are less SQL-like than SOQL).

4. **`frontend/public/data_sources_icons/servicenow.png`** — brand icon;
   filename matches the type so `DataSourceIcon.vue`'s convention fallback
   picks it up with no code change.

5. **`backend/tests/unit/test_servicenow_client.py`** (mocked `requests`, in
   the style of `test_druid_client.py` / `test_qlik_sense_client.py`):
   - `test_connection` success / 401 / bad-URL paths
   - `get_schema` parses sys_dictionary incl. inherited fields and reference→fk
   - `execute_query`: valid JSON spec → correct URL/params; pagination across
     pages; limit cap; malformed spec → clear error
   - display_values on/off parameter propagation
   - Optionally register a live-instance smoke config in
     `backend/tests/integrations/ds_clients.py` (ServiceNow gives free
     Personal Developer Instances, so a real integration target is easy).

6. **`CHANGELOG.md`** — feature entry.

Nothing needed in the agent layer: `app/ai/agents/data_source/data_source.py`
(summary + conversation starters) works generically off any client.

## 5. Phasing

- **Phase 1 (MVP)** — everything in §4: basic auth, curated tables +
  sys_dictionary discovery, Table API JSON-spec queries with pagination,
  unit tests, icon, registry entry at `version="beta"`.
- **Phase 2** — Aggregate/Stats API for group-by queries; OAuth
  (`oauth_app` variant, maybe API key); `sys_choice` choice-list values in the
  prompt schema; promote to `version="1.0.0"`.
- **Phase 3 (as demand appears)** — per-user delegated OAuth for ACL-faithful
  querying; knowledge-base articles as a document-shaped source; attachment
  retrieval.

## 6. Open questions

1. **Default table set** — is ITSM (incident/change/problem/catalog) the right
   default, or do target users care more about CMDB or HRSD tables? (Config
   override makes this low-stakes, but the default drives the demo experience.)
2. **Auth priority** — is basic auth acceptable for the first customers, or is
   OAuth a hard requirement (some hardened instances disable basic auth)?
3. **Aggregate API in phase 1?** — "how many P1 incidents this month" style
   questions will otherwise pull raw rows and aggregate in pandas, which works
   but is wasteful on large tables.
