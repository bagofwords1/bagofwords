# Feedback Loop — Priority ERP connector

Adds a **Priority ERP** (Priority Software) data source connector: a
`DataSourceClient` subclass speaking Priority's OData v4 REST API, with
config/credentials schemas, a `REGISTRY` entry, per-tenant OAuth wiring, and a
protocol-level mock so the whole path is verifiable without a licensed tenant.

Priority is an ERP queried over **OData**, not SQL, and its service root is
per-tenant. The closest existing references are `servicenow_client.py`
(per-tenant REST + delegated OAuth derived from config) and `powerbi_client.py`
(catalog crawl, dual-mode credentials, `test_connection` that classifies
failures by which layer answered) — *not* a SQL client.

Design background, including why a native connector rather than an MCP preset
and why direct SQL to the Priority database was rejected, is in
`docs/priority-erp-connector-analysis.md`.

## What was added

| Layer | File | Change |
|-------|------|--------|
| Client | `backend/app/data_sources/clients/priority_erp_client.py` | New `PriorityErpClient(DataSourceClient)` |
| Config | `backend/app/schemas/data_sources/configs.py` | `PriorityErpConfig`, `PriorityErpPatCredentials`, `PriorityErpBasicCredentials` |
| Registry | `backend/app/schemas/data_source_registry.py` | `"priority_erp"` entry: `data_shape="tables"`, `catalog_nouns=("form","forms")`, `version="beta"` |
| OAuth | `backend/app/services/connection_oauth_service.py` | `priority_erp` branch + `_priority_domain()` helper |
| Driver | — | none: plain `requests` (already a dependency) |
| Icon | `frontend/public/data_sources_icons/priority_erp.png` | Priority brand mark |
| Mock | `tools/priority/mock_server.py` | Protocol-level Priority OData service |
| Tests | `backend/tests/unit/test_priority_erp_client.py` | 32 tests driven over real HTTP against the mock |
| Tests | `backend/tests/unit/test_connection_oauth.py` | 7 Priority OAuth cases |

## Design decisions

- **Form = table.** `get_schemas()` reads `$metadata` once — Priority returns
  the entire tenant schema in a single request, unlike Power BI's per-dataset
  fan-out. Each Priority form becomes a `Table`, each form column a
  `TableColumn`, `<Key>` becomes `pks`, and navigation properties (subforms)
  become `fks` so join paths are visible.

- **Titles come from annotations, not properties.** This is the single most
  important parsing detail. Priority's `<Property Name= Type=>` carries **no**
  human label; the business vocabulary lives in
  `<Annotation Term="Priority.OData.Description" String="Customer Number"/>`.
  A parser reading only `<Property>` yields a catalog of `CUSTNAME`/`ORDNAME`
  with no labels — technically working, practically useless. `Priority.OData.Mandatory`
  (v25.1+) is captured alongside it.

- **Titles are localized.** A tenant's titles follow its environment language,
  so form/column **names** are always the identifiers and titles are only ever
  descriptions. The mock's `PART` form carries Hebrew titles specifically to
  keep that honest.

- **No table-level metadata block.** Following the rule set in
  `docs/table-metadata-context-gap.md` — only surface what isn't recoverable —
  Priority needs none: queries address the form name (which *is* `Table.name`),
  subforms are already in `fks`, and company is a connection property. Column
  titles reach the agent through the generic `description` attribute with no
  renderer change.

- **Curated default form set.** A tenant can carry thousands of forms and a
  fresh connection has no usage data to rank them by, so the default indexes a
  curated set (`ORDERS`, `CUSTOMERS`, `PART`, …). `discover_all` indexes
  everything; an explicit `forms` list overrides both. If a tenant shares none
  of the curated names (heavily customized, or non-English), the client falls
  back to indexing everything rather than producing an empty catalog.

- **Auth: three modes, deployment decides.** PAT sends
  `Basic base64("<PAT>:PAT")` — token in the *username* position, literal
  `PAT` as the password. Basic uses a dedicated API user from the Personnel
  File. Per-user OAuth is **on-premise only** (Priority scopes its OAuth2 guide
  to "on-prem (non-SaaS) installations") and needs the paid External ID module;
  Priority Cloud has no OAuth, so per-user there means bring-your-own PAT — the
  `zabbix` pattern. A Bearer token always wins over the service credential so
  queries run as the signed-in user.

- **OAuth endpoints are per-tenant.** `PRIORITY_DOMAIN` is "whatever comes
  before the `odata` segment" of the service root, so the branch derives
  `…/accounts/connect/{authorize,token}` from **config**, ServiceNow-style,
  rather than from constants. A sub-path before `/odata` is preserved for
  on-prem IIS virtual directories. Scope is `openid rest_api` and the client
  secret goes in the Basic header (`client_secret_basic`) — Priority rejects a
  body-carried secret, and always issues a secret, so unlike ServiceNow there
  is no public-client mode.

- **Rate limiting is configurable, not hardcoded.** Priority Cloud allows 100
  calls/minute/user and answers 429; on-premise has no documented ceiling, so
  `max_calls_per_minute=0` disables throttling rather than pacing a server that
  never asked.

- **`$apply` is refused with an actionable message.** Priority documents no
  server-side aggregation. The agent reaches for it naturally, so a bare
  "501 Not Implemented" is replaced with the limitation *and* the workaround,
  and the system prompt states it up front.

## Mock

Priority is licensed software with no public sandbox (the documented demo
tenant is auth-gated), so `tools/priority/mock_server.py` stands in at the
protocol level. It is deliberately faithful to the details that shape the
client, so a green test is real evidence rather than a tautology:

- `$metadata` puts titles in **annotations**, never on `<Property>`
- PAT auth accepts only `Basic base64("<PAT>:PAT")`; other shapes 401
- `$apply` returns **501** rather than silently succeeding
- `GetMetadataFor(entity=…)` serves single-form metadata (v25.0+)
- subforms are navigation properties reachable via `$expand`
- 429 past 100 calls/minute, like Priority Cloud
- `PART` carries Hebrew titles; `ACME_PROJECTS` is a customer-specific form
  visible only with `discover_all`

```bash
uv run python tools/priority/mock_server.py --port 8901
# service_root = http://127.0.0.1:8901/odata/Priority/tabmock.ini/demo
# PAT          = DEMOTOKEN     (or API user demo/demo)
```

## Verification

Tests boot the mock in a background uvicorn thread and drive the real client
over HTTP — hermetic, no external process, but still exercising the parts that
only break on the wire.

```bash
cd backend
BOW_ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
TESTING=true ENVIRONMENT=production \
  uv run pytest tests/unit/test_priority_erp_client.py tests/unit/test_connection_oauth.py -q
```

Observed: **32 passed** (connector) and **7 passed** (Priority OAuth cases).

### What the agent ends up seeing

The point of the connector — cryptic Priority column names arriving with their
business titles, composite keys intact, and the subform join path visible:

```xml
<table name="ORDERS" cols="6" description="Customer Orders">
<columns>
<column name="ORDNAME" dtype="Edm.String" description="Order Number"/>
<column name="CUSTNAME" dtype="Edm.String" description="Customer Number"/>
<column name="CDES" dtype="Edm.String" description="Customer Name"/>
<column name="CURDATE" dtype="Edm.DateTimeOffset" description="Order Date"/>
<column name="TOTPRICE" dtype="Edm.Decimal" description="Total Price"/>
<column name="ORDSTATUSDES" dtype="Edm.String" description="Order Status"/>
</columns>
<pks><pk name="ORDNAME" dtype="Edm.String"/></pks>
<fks><fk column="ORDNAME" ref_table="ORDERITEMS" ref_column="ORDNAME"/></fks>
</table>
```

### Manual checks run against the mock

| Check | Result |
|---|---|
| `test_connection` with PAT | `Connected to Priority. 5 form(s) visible in $metadata.` |
| `test_connection` with API user | success |
| Wrong PAT | classified as auth failure, names the API-licence and External-ID causes |
| No credentials | reported, not raised |
| Non-Priority URL | rejected before any request |
| Hebrew titles (`PART`) | `פריטים` / `מק"ט` intact |
| `discover_all` | surfaces `ACME_PROJECTS` |
| `$filter`+`$select`+`$orderby` | correct rows and column projection |
| `$expand` subform | nested list collapsed to a count |
| `$apply` | actionable "not supported, aggregate in code" error |
| `GetMetadataFor` fallback | resolves a non-curated form |
| OAuth derivation | `https://priority.acme.local` and IIS sub-path both correct |
| `construct_client` simulation | `oauth_*` stripped; every real param accepted |

## Not covered

- **No live tenant.** Everything above is against the mock. The real `$metadata`
  size and shape, actual `$apply` behaviour, and annotation coverage across a
  full tenant remain unverified — a tenant URL + PAT would close that.
- **Read-only.** Writes need Priority "transaction packages" licensing and the
  `confirm: true` policy path; out of scope here.
- **No UI pass.** The connect form renders from the config/credential schemas
  and the icon resolves by convention (`/data_sources_icons/priority_erp.png`),
  but this was not driven through the browser.
