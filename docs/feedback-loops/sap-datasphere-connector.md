# Feedback Loop — SAP Datasphere semantic-layer connector (new)

Validates the new `sap_datasphere` data source connector end to end: schema-driven
admin form → OAuth (client-credentials Technical User) → catalog discovery of
analytic models → semantic `$metadata` parsing (measures vs. dimensions) →
server-side-aggregated analytical OData queries → a real agent question answered
through the UI.

## Why a new connector (not the SAP HANA one)

SAP Datasphere's **semantic layer** — Analytic Models with measures, dimensions,
hierarchies, restricted/calculated measures and exception aggregation — is only
reachable over the **OData Consumption API** (analytical endpoint). The existing
`sap_hana` connector reaches Datasphere's *raw SQL / Open-SQL* views over the
HANA SQL port, which returns flat tables and cannot see analytic models or apply
their aggregation. The two are complementary: `sap_hana` = raw SQL,
`sap_datasphere` = governed semantic layer with per-user identity.

Datasphere is **cloud-only SaaS** — there is no Docker/on-prem build — so the
live loop runs against a protocol-faithful **mock OData server** plus, for the
`$apply`/`$select` query shape, the public TripPin OData v4 service. A real
tenant is reserved for a one-time Loop D sign-off (see "Known limits").

## What was added

- `backend/app/data_sources/clients/sap_datasphere_client.py` — OData client.
  Auth is dual-mode: a **Technical User** (`client_credentials`) for discovery/
  indexing and shared queries, or a per-user delegated `access_token`
  (authorization_code) injected by the connection layer. `get_schemas()` lists
  every consumption-exposed asset via the catalog API (`/catalog/assets`, one
  call across all authorized spaces, `@odata.nextLink` paging) and parses each
  asset's `$metadata` into `role=measure` / `role=dimension` columns (namespace-
  insensitive CSDL parsing; SAP measure annotations with a numeric-EDM-type
  fallback). `execute_query()` builds the analytical OData URL from
  `select`/`filter`/`orderby`/`top` (or a raw query string), supports analytic-
  model variables via `(P='v')/Set`, follows paging, and honors `max_rows`. The
  `description`/`system_prompt` teach the agent the `$select`-driven aggregation
  model (measures aggregate over the selected dimensions — no SQL/DAX/MDX).
- `SapDatasphereConfig` / `SapDatasphereCredentials` in
  `backend/app/schemas/data_sources/configs.py`; registry entry (type
  `sap_datasphere`, category `bi`, dual auth `technical_user` [system] + `oauth`
  [user]) in `backend/app/schemas/data_source_registry.py`.
- `sap_datasphere` OAuth branch in
  `backend/app/services/connection_oauth_service.py` (tenant-specific
  authorize/token URLs from config, interactive `oauth_client_id/secret` with
  fallback to the technical-user client) — this lights up the existing
  authorization-code + PKCE + refresh + per-user-token machinery for free.
- `frontend/public/data_sources_icons/sap_datasphere.png` (SAP logo) +
  `DataSourceIcon.vue` mapping (removed the old `sap_datasphere → sap_hana`
  alias so the new tile uses its own brand icon).
- `backend/tests/unit/test_sap_datasphere_client.py` (23 tests, including the
  `execute_query(query, table_name)` positional contract — see Loop C).
- `tools/datasphere/mock_server.py` — identity-aware mock Datasphere: OAuth
  token endpoint + catalog + analytical OData with real server-side aggregation
  and SAP measure annotations in `$metadata`. A DAC-protected model returns an
  empty result to the technical user and rows to a per-user token — reproducing
  the documented Datasphere behavior.
- `tools/agent/sap_datasphere_e2e.mjs` / `sap_datasphere_full_e2e.mjs` —
  Playwright UI drivers.

## Loop A — deterministic reproduction (no external services)

Mock the HTTP boundary (a URL-dispatching fake session); runs in a clean sandbox.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/unit/test_sap_datasphere_client.py -q          # 21 passed
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('sap_datasphere'))"
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q   # 11 passed
```

Covers: client-credentials grant (performed + cached) vs. per-user token
short-circuit; catalog → one Table per model; measure/dimension classification
(SAP annotation AND numeric fallback); space filter; `$select`/`$filter`/
`$orderby`/`$top` URL building; `(P='v')/Set` parameters; `@odata.nextLink`
paging; `max_rows`; DAC-empty → empty DataFrame; `test_connection` classification.

## Loop B — live HTTP round-trip (mock server, no real tenant)

```bash
uv run python tools/datasphere/mock_server.py --port 8899 &        # from repo root
```

Then, against the running mock, the client performs a real OAuth → bearer →
catalog → `$metadata` → aggregated query round-trip. Observed:

- `test_connection()` → `Found 3 exposed asset(s)`.
- discovery → `SALES/SalesAnalyticModel` (dims Country/Product/Year; measures
  Revenue/Quantity), `FINANCE/ExpensesModel`, `HR/SalariesModel`.
- `execute_query(table_name="SALES/SalesAnalyticModel", select="Country,Revenue",
  orderby="Revenue desc")` → server-side aggregation: **US 3500, DE 1550, JP
  1100, FR 1000** (US = 1200+800+1500).
- DAC-protected `HR/SalariesModel` → **empty** for the technical user, **rows**
  for a per-user token (`tok::user:alice`).

## Loop C — live UI pass (real Playwright, real stack, Claude Haiku)

`tools/agent/boot_stack.sh --dev` + `seed_org.py` + an Anthropic/Haiku LLM
provider (via `POST /api/llm/providers`), mock server on :8899, then
`tools/agent/sap_datasphere_full_e2e.mjs` drives the real UI end to end:

catalog search "datasphere" → **SAP Datasphere** tile (SAP brand icon) →
schema-generated form (host / token URL / Client ID / Client Secret) → **Test
Connection: "Connected successfully. Found 3 tables."** → create → **Schema
discovery: "Discovered 3 tables in 0s"** → Select Tables (all 3 models) → Set
Context (LLM-generated agent description) → agent created → **New report** → ask
*"What is the total revenue by country?"* → the agent (Claude Haiku) calls the
connector and returns the aggregated table:

```
Country | Total Revenue
US      | 3,500     (= 1200+800+1500, aggregated server-side)
DE      | 1,550     (= 600+950)
JP      | 1,100
FR      | 1,000     (= 700+300)
```

Run: `tools/agent/sap_datasphere_full_e2e.mjs` (whole flow) or
`sap_datasphere_query_only.mjs` (query against an existing agent). Evidence in
`media/sap-datasphere/`:

- `01-catalog-datasphere.png` — SAP Datasphere tile under "BI & analytics".
- `04-test-connection-result.png` — schema-generated form; "Connected
  successfully. Found 3 tables."
- `07-indexing-discovered.png` — "Discovered 3 tables in 0s".
- `08-tables-selected.png` — 3/3 analytic models activated.
- `13-running.png` — Haiku: "Creating Data · SALES/SalesAnalyticModel".
- `14-answer.png` — the aggregated revenue-by-country table.

### Bug this loop caught (why UI E2E, not just unit tests)

The agent's first run failed with
`QueryCapturingClientWrapper.execute_query() missing 1 required positional
argument: 'query'`. The framework wraps every client in a wrapper whose
`execute_query(query, *args, **kwargs)` requires `query` **positionally** (to
capture it for logging/quota) — but the client's `system_prompt` had steered the
agent to keyword-only calls (`table_name=`, `select=`), so no positional query
was passed. Unit tests missed it because they call the client directly, not
through the wrapper. Fix: the query builder now leads with the positional OData
string (`execute_query("$select=…", "Space/Model")`, Power BI's shape), tolerates
the model name being passed as the first arg, and a regression test locks the
positional contract.

## Known limits / follow-ups (Loop D — real tenant)

Only a live Datasphere tenant can confirm SAP-side behavior (a handful of
authenticated calls):
- that SAP **enforces DAC/RLS** per token as the mock simulates;
- the exact analytical `$metadata` **annotation vocabulary** (whether per-measure
  default aggregation is machine-readable) — the client parses SAP's documented
  terms with a numeric-type fallback;
- whether the catalog **lists vs. hides** DAC-protected assets to the technical
  user (if hidden, per-user discovery uses the `UserConnectionTable` overlay,
  same as Power BI/Fabric OBO);
- the current path prefix (`/api/v1/dwc/...` vs. the newer
  `/api/v1/datasphere/consumption/...`, deprecation ~March 2027) — both are
  config-driven fields on the connection.
- Per-user **authorization_code** sign-in is wired through the shared OAuth
  engine and unit-tested at the `get_oauth_params` boundary; the interactive
  browser consent against a real IAS/XSUAA tenant is a Loop D check.
