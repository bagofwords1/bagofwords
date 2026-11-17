# Bag of Words connector architecture and precedents for a Glassbox (REST, non-SQL) connector

All sources are repo-relative paths in `/home/user/bagofwords` with line numbers (`path:line`). Line numbers were read on 2026-11-17 from the working tree. Nothing in the repo was modified apart from this notes file.

## 1. What the add-connection-type skill and the AGENTS guides prescribe

### Takeaway
A connector is registry-driven: one `REGISTRY` entry plus a client class and Pydantic config/credentials schemas produce the form, auth variants and client resolution; frontend form code is almost never touched. The skill prescribes six file steps, four command-line verification steps, a live UI pass with screenshots, and a recorded feedback-loop doc.

### Cited Findings
- Connector = one entry in `backend/app/schemas/data_source_registry.py` (`REGISTRY`); "You almost never touch frontend form code" — `.agents/skills/add-connection-type/SKILL.md:8-11`
- Read one reference implementation end to end first (`postgresql`, `mssql_client.py`, or `clickhouse`) — `.agents/skills/add-connection-type/SKILL.md:16-18`
- Step 1, client: `backend/app/data_sources/clients/<type>_client.py` extending `clients/base.py`; `get_schemas()` (optional `progress_callback` kwarg), `execute_query(...)` (base provides a `query` alias), test-connection behaviour; keep calls sync because the base provides async wrappers — `.agents/skills/add-connection-type/SKILL.md:22-32`
- Step 2, schemas: `backend/app/schemas/data_sources/configs.py` gets a `<Type>Config` and one `<Type>...Credentials` class per auth variant; "These Pydantic schemas ARE the frontend form", rendered in `ConnectForm.vue` via `GET /available_data_sources` — `.agents/skills/add-connection-type/SKILL.md:33-38`
- Step 3, registry: `type`, `title`, `description`, `config_schema`, `credentials_auth=AuthOptions(default=..., by_auth={...})`, each `AuthVariant` with `scopes=["system","user"]` ("user" only if per-user credentials make sense), always an explicit `client_path`, `dev_only=True` while incubating — `.agents/skills/add-connection-type/SKILL.md:39-47`
- Step 4, driver dependency: `cd backend && uv add <driver>`; prefer pure Python; system libraries must go in the root `Dockerfile` and the PR description — `.agents/skills/add-connection-type/SKILL.md:48-51`
- Step 5, icon: `frontend/public/data_sources_icons/<type>.png|svg`, mapped in `frontend/components/DataSourceIcon.vue` — `.agents/skills/add-connection-type/SKILL.md:52-53`
- Step 6, tests: unit `backend/tests/unit/test_<type>_client.py` mocking the driver boundary only (reference `test_druid_client.py`); integration: add the type to `DATA_SOURCES` in `backend/tests/integrations/ds_clients.py`, with a `CONTAINER_REGISTRY` entry if a docker image exists, otherwise credentials in `integrations.json` (never committed; CI restores from `INTEGRATIONS_JSON_B64`) — `.agents/skills/add-connection-type/SKILL.md:54-63`
- Verification commands: `resolve_client_class('<type>')`; `pytest tests/unit/test_<type>_client.py`; `pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q`; `pytest tests/integrations/ds_clients.py -k "<type>"` — `.agents/skills/add-connection-type/SKILL.md:67-77`
- Live UI pass: create connection, "Test connection" succeeds, Tables Selector lists tables, run a prompt; screenshot the connect form and tables list (ui-evidence skill); in a DevBox use `bow-stack restart backend` instead of `boot_stack.sh`/`seed_org.py` — `.agents/skills/add-connection-type/SKILL.md:79-85`
- Record the loop as `docs/feedback-loops/<type>-connector.md` (sandbox-feedback-loop skill) — `.agents/skills/add-connection-type/SKILL.md:86-87`
- Pitfalls: missing `client_path` causes a silent dynamic-import fallback; decide the `user` scope deliberately; registry `description` and field descriptions are user-facing product copy; `is_connection=False` is only for tool providers, otherwise schema indexing skips the type — `.agents/skills/add-connection-type/SKILL.md:89-99`
- MCP alternative: an MCP server is added as an `McpPreset` in `MCP_PRESETS` resolving to `type="mcp"`, with no client, no schemas, no per-type tests; tests extend `backend/tests/unit/test_mcp_presets.py` — `.agents/skills/add-connection-type/SKILL.md:101-108`, `:169-177`
- Root AGENTS: follow the matching skill before starting; UI changes under `frontend/{pages,components,layouts,assets}/**` need mandatory before/after screenshots (ui-evidence); release-notes skill bumps `VERSION` and `CHANGELOG.md`; docs-update skill refreshes docs.bagofwords.com — `AGENTS.md` ("Agent skills" section)
- Root AGENTS DevBox table: if `BOW_SANDBOX_DEVBOX=1`, never start/kill the stack yourself, use `bow-stack`; do not run `uv sync --extra dev` (use `uv sync --no-install-project --extra kerberos --extra dev` after dependency edits); `--db=sqlite` is the only safe pytest DB mode by default — `AGENTS.md` ("DevBox" section)
- Root AGENTS i18n: locale catalogs must have identical shape or the sync check fails; the root guide names `en`, `es`, `he`, but the directory holds ten catalogs (`ar, de, en, es, fr, he, it, pt, ru, sv`) and the backend guide lists the same ten as `enabled_locales` — `AGENTS.md` ("Locale / i18n" section); `backend/AGENTS.md` ("Locale / i18n" section, `I18nConfig` bullet); directory listing of `locales/`
- Backend layering: `routes` → `services` → `models`/`data_sources`; `data_sources/` holds connectors; `schemas/` holds Pydantic models — `backend/AGENTS.md` ("Structure", "Module roles and boundaries")
- Backend LLM prompt rule: code/artifact agents (coder, data_source) stay English so identifiers, SQL and JSON fields are not translated — `backend/AGENTS.md` ("LLM prompts" bullet)
- Test rules: mock at boundaries only (LLM providers, OAuth, external drivers ODBC/HTTP, SMTP, clocks); test the contract not the implementation; assert invariants not incidental strings; every test must be able to fail; no sleeps; unit tests that open a DB session need `@pytest.mark.db` — `backend/tests/AGENTS.md` ("Anti-overfit rules" 1-9, "Layout & how to run")
- Test placement: needs real third-party credentials → `integrations/`; pure logic → `unit/`; new endpoint or flow → `e2e/` — `backend/tests/AGENTS.md` ("Placement guide")

### Inferences
- The skill text is written around SQL databases (driver, ODBC, docker image); for an HTTP SaaS the "driver boundary" to mock is `requests`, which is what the REST connectors' unit tests do (see section 6).
- The skill does not mention several cross-cutting registrations that recent REST connectors needed (egress matrix row, literal-rule table, README row, simulator under `tools/`); those are documented in sections 6 and 8 from the code and commit history.

### Gaps
- `docs/design/i18n.md`, the ui-evidence, sandbox-feedback-loop, release-notes and docs-update skill files were not opened; only their summaries in `AGENTS.md` were read.

## 2. Registry, client base class, schemas: locations and required methods

### Takeaway
The contract is `DataSourceClient` in `clients/base.py` (six abstract members: `description`, `test_connection`, `get_schemas`, `get_schema`, `prompt_schema`, `execute_query`), a `DataSourceRegistryEntry` in `REGISTRY`, and Pydantic classes in `configs.py`. The client is instantiated with config and credentials merged into keyword arguments filtered to the constructor signature, so schema field names must equal `__init__` parameter names.

### Cited Findings
- Base class `DataSourceClient(DiscoveryClient, ABC)` — `backend/app/data_sources/clients/base.py:116`
- Abstract members: `description` property (`:165-168`), `test_connection` (`:170-172`), `get_schemas` (`:174-176`), `get_schema(table_name)` (`:178-180`), `prompt_schema` (`:182-184`), `execute_query(**kwargs)` (`:186-188`) — `backend/app/data_sources/clients/base.py`
- `query()` is an alias for `execute_query` because generated code often calls `.query(...)` — `backend/app/data_sources/clients/base.py:190-198`
- Default `capabilities = {Capability.QUERY}`; file, mail, note, chat and browser capabilities are separate enum values — `backend/app/data_sources/clients/base.py:13-80`, `:121`
- `relative_date_hint` (default `None`) is rendered next to `description` in the `<connection_clients>` block; the comment says `None` is right for non-SQL sources "(mail, files, metrics APIs)" — `backend/app/data_sources/clients/base.py:123-132`
- `sql_literal_style` (default `"standard"`) controls how query parameters are quoted; `None` refuses parameters — `backend/app/data_sources/clients/base.py:134-142`
- Async wrappers: `atest_connection` (`:202-217`), `aget_schemas` forwards `progress_callback`, `prior_catalog`, `prior_tables`, `force_refresh`, `section_sink` only if the sync method's signature accepts them (`:219-258`), `aget_schema`, `aprompt_schema`, `aexecute_query`, `aquery` (`:260-271`) — `backend/app/data_sources/clients/base.py`
- `aget_schemas` passes the result through `require_complete_discovery` — `backend/app/data_sources/clients/base.py:258`
- Return shapes seen in practice: `test_connection` returns `{"success": bool, "message": str}` — `backend/app/data_sources/clients/posthog_client.py:190-224`, `backend/app/data_sources/clients/crowdstrike_client.py:442-465`
- `get_schemas` returns either a list of plain dicts with `name`, `columns[{name,dtype}]`, `pks`, `fks` (PostHog) or a list of `Table` models (CrowdStrike) — `backend/app/data_sources/clients/posthog_client.py:36-54`, `:226-229`; `backend/app/data_sources/clients/crowdstrike_client.py:490-508`
- `Table` fields: `name`, `description`, `columns`, `pks`, `fks`, `is_active`, connection info, `metadata_json`; `TableColumn`: `name`, `dtype`, `description`, `metadata`; `ForeignKey`: `column`, `references_name`, `references_column` — `backend/app/ai/prompt_formatters.py:196-231`
- `execute_query` returns a `pandas.DataFrame` — `backend/app/data_sources/clients/posthog_client.py:297-308`, `backend/app/data_sources/clients/crowdstrike_client.py:535-566`
- `prompt_schema` conventionally returns `ServiceFormatter(self.get_schemas()).table_str` for API services — `backend/app/data_sources/clients/posthog_client.py:310-312`, `backend/app/data_sources/clients/crowdstrike_client.py:686-687`; `ServiceFormatter` at `backend/app/ai/prompt_formatters.py:308-322`
- `@discovery_progress` decorator and `discovery_items` helper wrap discovery — `backend/app/data_sources/clients/progress.py:136`, `:168`; used at `backend/app/data_sources/clients/posthog_client.py:226`, `backend/app/data_sources/clients/crowdstrike_client.py:490-498`
- Registry models: `AuthVariant` (`title`, `schema`, `scopes`, `overlay`, `catalog_ownership`) — `backend/app/schemas/data_source_registry.py:326-344`; `AuthOptions` (`default`, `by_auth`) — `:347-358`; `SetupStep` (`title`, `body`, `code`, `language`) — `:361-376`
- `DataSourceRegistryEntry` fields: `type`, `title`, `description`, `setup_guide`, `status`, `version`, `deprecated`, `config_schema`, `credentials_auth`, `client_path`, `dev_only`, `requires_module`, `is_document_based`, `requires_license`, `is_connection`, `data_shape` (`tables|files|objects|tools`), `catalog_ownership` (`shared|per_user|none`), `ui_form` (`data_source|integration|mcp|custom_api`), `catalog_nouns`, `category` (`databases|bi|infra|services|files|custom`, default `databases`) — `backend/app/schemas/data_source_registry.py:379-461`
- `REGISTRY` dict starts at `backend/app/schemas/data_source_registry.py:645`; it holds 105 entries (count of `"<key>": DataSourceRegistryEntry` lines)
- `list_available_data_sources` serialises `type`, `title`, `description`, `config` class name, `status`, `version`, `requires_license`, `is_connection`, `data_shape`, `catalog_nouns`, `catalog_ownership`, `ui_form`, `category`, `setup_guide` for the add-connection grid — `backend/app/schemas/data_source_registry.py:3200-3231`
- `resolve_client_class` imports `client_path`; on failure logs and falls back to `app.data_sources.clients.<type>_client.<TitleCase>Client` — `backend/app/schemas/data_source_registry.py:3447-3477`
- Helper lookups: `config_schema_for` (`:3327`), `requires_no_credentials` (`:3331`), `supports_user_auth` (`:3344`), `credentials_schema_for` (`:3390`), `tool_provider_types` (`:3399-3406`), `data_shape_for` (`:3420`), `catalog_nouns_for` (`:3433`) — `backend/app/schemas/data_source_registry.py`
- Config/credentials classes live in `backend/app/schemas/data_sources/configs.py` (5,197 lines); fields use `Field(..., title=, description=, json_schema_extra={"ui:type": "string|password|select|boolean"})`, with `"ui:options"` for selects — `backend/app/schemas/data_sources/configs.py:1097-1136` (CrowdStrike), `:2513-2534` (PostHog)
- Client construction: `ConnectionService.construct_client` merges config and resolved credentials into one dict, strips meta keys and keys starting with `oauth_`, narrows to the constructor signature unless it takes `**kwargs`, runs `acheck_connection_params(connection.type, params)` (egress policy), then calls `ClientClass(**allowed)` — `backend/app/services/connection_service.py:2814-2874`
- Egress destination check reads host keys `host, hostname, server, server_hostname` and URL keys including `url, base_url, server_url, site_url, instance_url, api_url` — `backend/app/core/egress.py:718-720`, `:763-764`
- Other `resolve_client_class` call sites: `backend/app/ai/agent_v2.py:242`, `backend/app/services/data_source_service.py:2842`, `:3131`, `:3524`, `:5207`, `backend/app/services/user_data_source_credentials_service.py:389`, `backend/app/models/connection.py:215`
- Routes: `GET /available_data_sources` (`backend/app/routes/data_source.py:35`), `GET /connectors/catalog` (`:43`), `GET /data_sources/{type}/fields` (`:124-132`), `POST /data_sources/test_connection` (`:197`)

### Inferences
- A Glassbox config field that carries the API host should be named with one of the recognised URL keys (for example `base_url`) so the egress policy validates it without a per-type exception; `_TYPE_HOST_KEYS` exists for types that use other names (`backend/app/core/egress.py:729`).
- Because the constructor receives only keyword arguments that match its signature, a mismatch between a schema field name and an `__init__` parameter is silently dropped rather than raising.

### Gaps
- `get_data_source_fields` in `data_source_service.py` (how the JSON schema is shaped for the form) was not read.
- `DiscoveryClient` / `require_complete_discovery` semantics (`backend/app/data_sources/clients/discovery.py:41-58`) were located but not read; whether an empty catalog is rejected is not confirmed.
- The full list of `CONNECTION_URL_KEYS` beyond line 720 was not read.

## 3. Existing non-SQL / SaaS-API connectors and how each works

### Takeaway
Two patterns exist for REST SaaS sources: (a) the vendor exposes a query language over HTTP and the client passes the string through (PostHog HogQL, Salesforce SOQL); (b) the vendor has only resource endpoints and the client defines a static "virtual table" catalog plus a JSON query spec that it translates into paged API calls (CrowdStrike, AppDynamics, Monday, ServiceNow). There is also a generic `custom_api` tool provider and MCP presets, which expose tools, not tables.

### Cited Findings
**PostHog (product analytics, HogQL over HTTP)**
- Registry: `category="services"`, description "Product analytics platform - query events, users, sessions, and more with HogQL.", single auth variant `api_key` ("Personal API Key") with `scopes=["system","user"]`, `version="beta"` — `backend/app/schemas/data_source_registry.py:1701-1719`
- Config: `host` (default `https://us.posthog.com`), `project_id`; credentials: `api_key` (`ui:type: password`) — `backend/app/schemas/data_sources/configs.py:2513-2534`
- Schema is a hard-coded list `POSTHOG_SCHEMAS` of seven tables (`events`, `persons`, `sessions`, `person_distinct_ids`, `groups`, `cohort_people`, `session_replay_events`) returned unchanged by `get_schemas` — `backend/app/data_sources/clients/posthog_client.py:35-149`, `:226-229`
- Auth is a bearer header on a `pinned_session()` — `backend/app/data_sources/clients/posthog_client.py:172-188`
- `test_connection` does `GET {host}/api/projects/{project_id}/` and maps 200/401/404 to messages — `backend/app/data_sources/clients/posthog_client.py:190-224`
- Query: `POST {base_url}/query/` with `{"query": {"kind": "HogQLQuery", "query": ...}}`, timeout 120 s — `backend/app/data_sources/clients/posthog_client.py:246-263`
- Rate limiting: 429 retried up to 3 times, wait taken from `Retry-After`, clamped to 1-60 s, default 5 s — `backend/app/data_sources/clients/posthog_client.py:14-21`, `:267-275`, `:405-417`
- No pagination in `execute_query`; bulk extraction pages with LIMIT/OFFSET via a separate `PostHogSource` — `backend/app/data_sources/clients/posthog_client.py:24-32`, `:166-170`; `backend/app/data_sources/fast/posthog_source.py`
- `sql_literal_style = "doubled"` — `backend/app/data_sources/clients/posthog_client.py:155-157`
- Prompt text: `description` = one-line summary + `system_prompt()` with example `client.execute_query('''...''')` calls, table list and property-access notes — `backend/app/data_sources/clients/posthog_client.py:314-402`
- Alias `PosthogClient = PostHogClient` for dynamic resolution — `backend/app/data_sources/clients/posthog_client.py:420-421`

**CrowdStrike Falcon (REST only, JSON query spec, virtual tables)**
- Registry: `category="infra"`, auth `api_client` ("API Client (OAuth)") with `scopes=["system","user"]`, explicit `client_path`, `version="beta"` — `backend/app/schemas/data_source_registry.py:1010-1024`
- Config: `region` select, optional `base_url`, `verify_ssl`; credentials: `client_id`, `client_secret` (password), optional `member_cid` — `backend/app/schemas/data_sources/configs.py:1097-1136`
- Virtual table catalog `_CATALOG` maps each table to `mode` (`combined` or `ids`), `path`, `paging` (`offset|token|after`), `page_max`, `sort_sep`, required `scope`, `pk`, `columns`, `fks`, `desc` — `backend/app/data_sources/clients/crowdstrike_client.py:66-110`
- `get_schemas` builds `Table` objects from the catalog and annotates tables whose scope the API client lacks; never fails discovery on the probe — `backend/app/data_sources/clients/crowdstrike_client.py:490-531`
- `test_connection` probes every table with `limit=1` and reports unreadable scopes — `backend/app/data_sources/clients/crowdstrike_client.py:442-486`
- `execute_query(query)` takes a JSON string or dict `{"table", "filter" (FQL), "sort", "fields", "params", "limit"}`, default 500 rows, plus an escape hatch `{"path": "/...", "params": {...}}` that issues a single GET — `backend/app/data_sources/clients/crowdstrike_client.py:535-566`
- `path` escape hatch is validated (must start with `/`, no `://`, no `..`, no leading `//`) — `backend/app/data_sources/clients/crowdstrike_client.py:678-681`
- Pagination: `_paginate` loops per-endpoint offset / token / `after` cursor up to `limit`, capping the page size at the endpoint maximum — `backend/app/data_sources/clients/crowdstrike_client.py:595-628`
- Rate limiting: up to 3 retries on 429, wait from `X-RateLimit-RetryAfter` or `Retry-After`, capped at 10 s; token refresh on 401 — `backend/app/data_sources/clients/crowdstrike_client.py:53-54`, `:360-387`, `:407-416`
- Prompt: `_PROMPT` documents the JSON spec, FQL syntax, semantics of codes, pandas-side aggregation and joins, and three examples; table descriptions are appended because "the indexer stores columns only" — `backend/app/data_sources/clients/crowdstrike_client.py:689-752`
- Relative dates are computed in Python at run time in the examples — `backend/app/data_sources/clients/crowdstrike_client.py:725-728`

**AppDynamics (REST, JSON query spec)**
- Registry: `category="infra"`, two auth variants `userpass` and `api_client`, both `scopes=["system","user"]`, explicit `client_path` with a comment that dynamic resolution would derive the wrong class name — `backend/app/schemas/data_source_registry.py:991-1009`
- `execute_query(query)` takes a JSON spec `{"table", "application", "metric_path", "duration_in_mins" | start/end, "rollup", "params", "limit"}` — `backend/app/data_sources/clients/appdynamics_client.py:458-472`
- 429 is not retried; it raises "AppDynamics rate limit hit (429). Narrow the metric path or time range, or retry later." — `backend/app/data_sources/clients/appdynamics_client.py:350-354`
- Session held on the client via `pinned_session()` — `backend/app/data_sources/clients/appdynamics_client.py:232-234`
- `description` = summary + `system_prompt()` — `backend/app/data_sources/clients/appdynamics_client.py:237-243`

**Monday data (GraphQL, JSON query spec)**
- Registry: `category="services"`, auth variants `api_token` (`scopes=["system","user"]`) and `oauth` "Sign in with monday.com" (`OAuthDelegatedCredentials`, `scopes=["user"]`) — `backend/app/schemas/data_source_registry.py:902-920`
- Module docstring: "There is no SQL endpoint; queries are JSON specs" with cursor pagination — `backend/app/data_sources/clients/monday_client.py:6`
- Throttling: up to 6 retries on 429, complexity exhaustion and 5xx — `backend/app/data_sources/clients/monday_client.py:53`, `:158-181`
- Cursor pagination via `next_items_page` — `backend/app/data_sources/clients/monday_client.py:851-860`
- A separate Monday MCP preset also exists — `backend/app/schemas/data_source_registry.py:3018-3020`

**ServiceNow (REST Table API, JSON spec, offset paging)**
- Registry: `category="services"`, `userpass` variant with `scopes=["system","user"]` — `backend/app/schemas/data_source_registry.py:882-892`
- Offset paging with `sysparm_limit` / `sysparm_offset`; 429 raises without retry — `backend/app/data_sources/clients/servicenow_client.py:295-296`, `:372`, `:645`
- Declares per-instance capabilities (adds file capabilities for attachments, can drop back to `{Capability.QUERY}`) — `backend/app/data_sources/clients/servicenow_client.py:204-256`

**Salesforce (SOQL passthrough via `simple_salesforce`)**
- Registry: `category="services"`, variants `jwt`, `client_credentials`, `userpass`, all `scopes=["system"]` — `backend/app/schemas/data_source_registry.py:866-880`
- Uses `simple_salesforce.Salesforce` with `session=pinned_session()` — `backend/app/data_sources/clients/salesforce_client.py:510-523`
- `sql_guard.py` lists "Salesforce SOQL, PostHog HogQL, Mongo, Qlik" under "Everything else" — `backend/app/ai/code_execution/sql_guard.py:21`

**NetSuite**
- Registry: `category="services"`, single `token` variant "Token-Based Auth" with `scopes=["system"]` — `backend/app/schemas/data_source_registry.py:821-828`; client at `backend/app/data_sources/clients/netsuite_client.py:100`, `:155`

**Google Analytics (client exists, appears not registered)**
- `GoogleAnalyticsClient(service_account_file, property_id)` uses `BetaAnalyticsDataClient`; `execute_query` takes keyword arguments `start_date`, `end_date`, `dimensions`, `metrics` instead of a query string — `backend/app/data_sources/clients/google_analytics_client.py:2-3`, `:16-17`, `:34`
- Schema is three hard-coded report "tables" ("Sessions Report", "Traffic Source Report", "User Demographics Report") — `backend/app/data_sources/clients/google_analytics_client.py:129-150`
- `description` tells the model to call `client.execute_query(start_date=..., end_date=..., dimensions=[...], metrics=[...])` — `backend/app/data_sources/clients/google_analytics_client.py:224-256`
- `GoogleAnalyticsConfig` / `GoogleAnalyticsCredentials` are imported in the registry (`backend/app/schemas/data_source_registry.py:84`, `:280`) but a search of that file for `google_analytics` returned no `REGISTRY` key

**Custom API (generic REST tool provider)**
- Registry: `category="custom"`, description "Connect to any REST API by defining endpoint schemas. Endpoints are exposed as callable tools.", auth variants `none`, `bearer`, `basic`, `api_key`, `oauth_app`; `is_connection=False`, `data_shape="tools"`, `catalog_ownership="none"`, `ui_form="custom_api"` — `backend/app/schemas/data_source_registry.py:2884-2929`
- `CustomApiPreset` / `CUSTOM_API_PRESETS` exist and resolve to `type="custom_api"`; one preset (`_X_WRITE_PRESET`) is registered — `backend/app/schemas/data_source_registry.py:3246-3324`

**MCP presets**
- `McpPreset` model with `category` defaulting to `services`; presets carry branding and a form spec only — `backend/app/schemas/data_source_registry.py:489-509`; `.agents/skills/add-connection-type/SKILL.md:101-108`

**HubSpot design doc (planned connector, most recent written reasoning about this choice)**
- The plan modelled `execute_query` on ServiceNow (JSON query spec) believing HubSpot had no query language; a live sign-in found the MCP server exposes SQL (`query_crm_data`), and the doc says to re-scope before building — `docs/design/hubspot-connector.md` (lines 1-45 read)
- The doc contrasts the two archetypes: MCP preset = `data_shape` `tools`, `catalog_ownership` `none`, `is_connection` `False`; native connector = `tables`, `shared`, `True` — `docs/design/hubspot-connector.md` ("This is not the MCP preset" table)
- The stated value of a native connector over an MCP preset is "the indexed catalog ... tracked queries and reports, and `data_shape="tables"` integration" — `docs/design/hubspot-connector.md` (superseded notice)

**Shared plumbing for HTTP connectors**
- All `requests`-based connectors obtain sessions from `app.core.egress.pinned_session` / `pinned_request`; a unit test fails any connector that calls `requests` directly — `backend/tests/unit/test_connector_egress.py:1-14`, `:243-245`; usage at `backend/app/data_sources/clients/posthog_client.py:6`, `:176`, `backend/app/data_sources/clients/crowdstrike_client.py:43`, `:303`

### Inferences
- Names searched for and not found as native clients in `backend/app/data_sources/clients/`: Mixpanel, Amplitude, Heap, Pendo, HubSpot (design doc and MCP icon only), Stripe, Zendesk, Jira (MCP preset icon mapping only), Datadog. This is from the directory listing, not an exhaustive content search.
- PostHog is the only registered product-analytics connector, but it is atypical as a template because it relies on a vendor SQL dialect; whether Glassbox offers a comparable query language is outside this repo and decides which pattern applies.

### Gaps
- Salesforce, Monday, ServiceNow and NetSuite clients were only outlined by grep; their `get_schemas` and prompt text were not read in full.
- Other REST connectors (`zabbix`, `splunk`, `aria_operations`, `intune`, `sisense`, `prometheus`, `aws_cloudwatch`) were not examined.
- Whether `google_analytics` is registered under a different key or deliberately retired was not determined.

## 4. How agent code generation consumes a client; where prompt hints live

### Takeaway
Generated Python receives a `ds_clients` dict and calls `ds_clients["<client_key>"].execute_query(<query>)`; the only per-connector prompt text is the client's own `description` property (plus the optional `relative_date_hint`), rendered into a `<connection_clients>` block. There is no separate prompt file per connector.

### Cited Findings
- `render_ds_client_entry` builds `client_key: ...\ndescription: {client.description}` and appends `relative_dates: {hint}` when `relative_date_hint` is set; it is the single renderer shared by every codegen prompt — `backend/app/ai/prompt_formatters.py:6-19`
- Coder prompt: "Each connection client may be SQL, document DB, service API, or Excel. You have a `ds_clients` dict ... Each ds_client has a method `execute_query("QUERY")` that returns data. The 'QUERY' depends on the data source type." followed by `<connection_clients>{data_source_section}</connection_clients>` — `backend/app/ai/agents/coder/coder.py:489-496`
- Same block repeats in other coder prompts — `backend/app/ai/agents/coder/coder.py:1021-1023`, `:1171-1173`, `:1328-1330`
- Generated code must use the exact `client_key` literal and only query tables listed under the matching `<connection name="...">` in `<ground_truth_schemas>` — `backend/app/ai/agents/coder/coder.py:536-539`
- Rolling date windows must be computed at execution time, via engine functions or Python `datetime` — `backend/app/ai/agents/coder/coder.py:109`
- Convention in API connectors: `description` returns a one-line summary plus `self.system_prompt()` — `backend/app/data_sources/clients/posthog_client.py:398-402`, `backend/app/data_sources/clients/crowdstrike_client.py:292-297`, `backend/app/data_sources/clients/appdynamics_client.py:237-243`
- CrowdStrike restates table semantics in the prompt because "the indexer stores columns only" — `backend/app/data_sources/clients/crowdstrike_client.py:689-693`
- Query-parameter quoting is keyed off the client's `sql_literal_style`; JSON-document queries refuse parameters by shape — `backend/app/data_sources/clients/base.py:134-149`; `backend/tests/unit/test_query_params.py:567-571`
- PostHog-specific wiring outside the client: table preview spec (`backend/app/services/table_preview_service.py:105`), custom-query allow-list (`backend/app/services/custom_query_service.py:110`), query language tag `"hogql"` (`backend/app/schemas/connection_schema.py:233`), extraction source (`backend/app/data_sources/fast/posthog_source.py`)
- CrowdStrike's introducing commit touched none of those services (file list in section 8) — `git show --stat 8e941c3d`

### Inferences
- A JSON-spec connector needs no changes under `backend/app/ai/`; the prompt contract is entirely inside the client file. Custom-table materialisation and table preview are optional extras that PostHog added later (`CHANGELOG.md:781`), not part of a first connector.
- Since `execute_query` is documented to the model as taking one "QUERY" argument, a single string/dict argument (JSON spec) fits the generic prompt better than the Google Analytics keyword-argument style.

### Gaps
- How `data_source_section` is assembled per connection in `coder.py`, and how the planner describes tables for `data_shape="tables"` service sources, was not traced.
- `backend/app/ai/code_execution/code_execution.py` (sandbox, how `ds_clients` is injected) was not read beyond one grep hit at `:2573`.

## 5. Frontend pieces, icons, i18n, docs

### Takeaway
The connect form is rendered from the registry and Pydantic schemas with no per-connector Vue code; the only frontend change normally needed is an icon file, and a PNG named `<type>.png` needs no mapping at all. Locale catalogs hold category labels and optional per-type setup-guide strings, not connector titles or field labels.

### Cited Findings
- `ConnectForm.vue` fetches `/available_data_sources` and `/data_sources/{type}/fields?auth_policy=system_only` — `frontend/components/datasources/ConnectForm.vue:594`, `:607`
- Field widgets are chosen from `ui:type` (also accepts `uiType`, `ui_type`, `ui`); there is a fallback for schemas without `ui:type`; `keyvalue`, `json`, `number` are special-cased — `frontend/components/datasources/ConnectForm.vue:144`, `:581-584`, `:655-676`
- Setup guide steps come from the registry entry and are localised when `data.setupGuides.<type>.<i>.{title,body}` exists — `frontend/components/datasources/ConnectForm.vue:277-285`; `backend/app/schemas/data_source_registry.py:361-376`
- `locales/en.json` has `setupGuides` with per-type arrays (`kubernetes`, `mecm`, ...) — `locales/en.json:4833-4850`
- Category labels are i18n keys `data.catDatabases`, `data.catBi`, `data.catInfra`, `data.catServices`, `data.catFiles` — `locales/en.json:4517-4522`; `frontend/components/AddConnectionModal.vue:398-402`
- A search of `locales/en.json` for `posthog` and `crowdstrike` returned no matches (only `setupGuides` matched the combined pattern)
- Icon resolution order: `CONNECTOR_ICON_FILE` by connector key (MCP presets) → `TYPE_ICON_FILE` for types whose asset is not `<type>.png` → tool icons under `/icons` → fallback `/data_sources_icons/${t}.png` — `frontend/components/DataSourceIcon.vue:89-103`, `:120-157`
- Existing assets: `posthog.png`, `crowdstrike.png`, `appdynamics.png`, `salesforce.png`, `prometheus.png`, `monday.svg` in `frontend/public/data_sources_icons/`
- CrowdStrike's introducing commit added only `frontend/public/data_sources_icons/crowdstrike.png` on the frontend side — `git show --stat 8e941c3d`
- Docs touched by recent connectors: `docs/feedback-loops/<type>-connector.md` (e.g. `docs/feedback-loops/crowdstrike-connector.md`, 108 lines added in `8e941c3d`); PR evidence screenshots under `media/pr/<type>-connector/`; optional design doc `docs/design/<type>-connector.md` (`docs/design/appdynamics-connector.md`, `docs/design/hubspot-connector.md`, `docs/design/zabbix-connector.md`)
- `README.md` has a connector table with a row per connector (`README.md:181` PostHog "Business app", `README.md:195` CrowdStrike Falcon "Security"); a past commit was needed to add 15 missing rows — `git log` `c5557f2b README: list the 15 connectors missing from Connect Any Data`
- `CHANGELOG.md` gets a user-facing line per connector — `CHANGELOG.md:240` (CrowdStrike), `CHANGELOG.md:1771` (PostHog)
- Catalog search aliases exist for connectors — `git log` `97162546 Pin Dremio to the egress policy; search connector aliases in the catalog`

### Inferences
- A Glassbox connector with a PNG icon named `glassbox.png` and no setup guide would require zero edits to Vue files and zero locale keys. If a `setup_guide` is added (plausible, since Glassbox API credentials must be minted in an admin console), English text lives in the registry and translations are optional keys under `data.setupGuides.glassbox`; if added to one catalog they must be added to all ten to keep catalog shapes identical.
- An SVG icon would require one line in `TYPE_ICON_FILE` (`frontend/components/DataSourceIcon.vue:120-145`).

### Gaps
- Where catalog search aliases are defined (commit `97162546`) was not located.
- Whether `AddConnectionModal.vue` has a hard-coded "Popular" list (`data.catPopular`, `locales/en.json:4828`) that a new connector could join was not checked.
- The external docs site (docs.bagofwords.com, via the docs-update skill) was not inspected.

## 6. Existing connector tests and the expected pattern

### Takeaway
A REST connector is expected to ship a unit test that fakes only the `requests` boundary with a small in-test server, a row in the egress matrix, an entry in the literal-rule table, a `DATA_SOURCES` entry for the integration harness, and (by recent precedent) a vendor API simulator under `tools/<vendor>/`.

### Cited Findings
- `backend/tests/unit/test_crowdstrike_client.py` docstring lists what it covers (region resolution, OAuth flow, per-endpoint paging and page maxima, sort syntax, required filters, flattening and projection, 429 back-off, spec validation, `get_schemas()` shape) and states "Only the `requests` boundary is faked: a small in-test Falcon server" — `backend/tests/unit/test_crowdstrike_client.py:1-16`
- The fake is a scripted API class (`FakeFalcon`) that applies the endpoint's paging and limit rules and validates each request — `backend/tests/unit/test_crowdstrike_client.py:58-70`
- Other REST unit tests present: `test_appdynamics_client.py`, `test_monday_client.py`, `test_prometheus_client.py`, `test_posthog_source.py`; fixtures directory `backend/tests/unit/fixtures/appdynamics/` — directory listing of `backend/tests/unit`
- No `test_posthog_client.py` exists (count 0 in `backend/tests/unit`)
- Egress matrix: every connector built on `requests` must have a row in `CASES`; each row runs the real client's `test_connection` over real sockets with the network played by `NetTrap` — `backend/tests/unit/test_connector_egress.py:1-14`, `:69-126`
- `test_every_connector_on_requests_has_a_row_in_the_matrix`: "A connector added without a row fails here" — `backend/tests/unit/test_connector_egress.py:269-272`
- `test_no_connector_calls_requests_directly` AST-scans client modules for unpinned `requests` calls — `backend/tests/unit/test_connector_egress.py:210-245`
- Row examples: PostHog `(api_key="k", host=u, project_id="1")` (`:75`), CrowdStrike `(client_id="i", client_secret="s", base_url=u)` (`:77`); vendor-fixed hosts pass a fixed name as the fourth element, e.g. Monday `"api.monday.com"` (`:123`) — `backend/tests/unit/test_connector_egress.py`
- Literal-rule table: "a new connector must be added here on purpose, not inherit 'standard' unseen"; JSON-document connectors are `"standard"` and refuse parameters by document shape — `backend/tests/unit/test_query_params.py:567-612` (CrowdStrike and AppDynamics entries at `:576`, `:583`; PostHog `"doubled"` at `:600`)
- Integration harness: `DATA_SOURCES` list in `backend/tests/integrations/ds_clients.py:28`; SaaS connectors run in "remote mode" with credentials from `integrations.json` pointing at a real tenant or the simulator — `backend/tests/integrations/ds_clients.py:78-92`
- The harness asserts a table count, so a connector whose `get_schemas()` returns `[]` cannot be listed — `backend/tests/integrations/ds_clients.py:25-27`
- Harness client factory uses the registry `client_path`, with a dynamic-name fallback — `backend/tests/integrations/ds_clients.py:664-677`; CrowdStrike keeps a `CrowdstrikeClient` alias for it — `backend/app/data_sources/clients/crowdstrike_client.py:755-757`
- Simulators: `tools/crowdstrike/{docker-compose.yaml,mock_falcon.py,stub_llm_crowdstrike.py}`, `tools/appdynamics/{docker-compose.yaml,mock_controller.py}` — directory listings; `git show --stat 8e941c3d`
- The CrowdStrike feedback loop states the simulator's routes and limits came from the official SDK and flags "Not verified against a real tenant" — `docs/feedback-loops/crowdstrike-connector.md:5-30`
- Generic e2e suites to keep green: `backend/tests/e2e/test_data_source.py`, `backend/tests/e2e/test_connection.py` — `.agents/skills/add-connection-type/SKILL.md:73-74`; both files exist in `backend/tests/e2e`
- Rule that governs all of this: mock at boundaries only, including "external data-source drivers (ODBC/HTTP)" — `backend/tests/AGENTS.md` (rule 4)

### Inferences
- The egress and literal-rule tests are enforcement tests: a new `requests`-based client will fail CI until both tables are updated, even though the skill file does not list them.
- For a vendor with a fixed API host the egress row cannot use the local server and gets only the redirect/name-change half of the check, as with Monday; if the Glassbox host is configurable (`base_url`), the row can use the local server like CrowdStrike.

### Gaps
- `_run_connection_test` (`backend/tests/integrations/ds_clients.py:714`) and `ds_kwargs` (`:470`) were not read; the exact `integrations.json` key shape is taken from the inline comments only.
- No e2e test specific to a single REST connector was looked for.
- No tests were executed in this investigation.

## 7. Existing mentions of "glassbox" in the repo

### Takeaway
There are none: no code, docs, changelog, design notes or commit messages mention Glassbox.

### Cited Findings
- Case-insensitive recursive search `grep -rIli glassbox .` (excluding `node_modules`, `.git`, `.venv`, `.nuxt`, `.output`) returned no files — run at repo root on 2026-11-17
- `git log --all --oneline -i --grep=glassbox` returned no commits
- The only occurrence of the word is the directory name `research_notes/Glassbox analytics integration research/` created for this research

### Inferences
- A Glassbox connector would be a greenfield addition with no prior design doc to reconcile with.

### Gaps
- GitHub issues, PRs and Linear were not searched (out of scope: local codebase only).

## 8. Assessment: closest precedents and file checklist for a Glassbox connector

### Takeaway
My assessment: model the client on CrowdStrike (static virtual-table catalog, JSON query spec, per-endpoint pagination, bounded 429 back-off, API-client auth), and use PostHog as the reference for product-analytics framing (registry `category="services"`, sessions/events table vocabulary, prompt examples). If Glassbox turns out to expose a SQL-like query endpoint, PostHog becomes the primary template instead.

### Cited Findings
- CrowdStrike's introducing commit `8e941c3d feat(connectors): add CrowdStrike Falcon data source` is the most complete recent worked example of a REST-only connector; it changed exactly: `backend/app/data_sources/clients/crowdstrike_client.py` (+756), `backend/app/schemas/data_source_registry.py` (+17), `backend/app/schemas/data_sources/configs.py` (+43), `backend/tests/integrations/ds_clients.py` (+7), `backend/tests/unit/test_crowdstrike_client.py` (+392), `docs/feedback-loops/crowdstrike-connector.md` (+108), `frontend/public/data_sources_icons/crowdstrike.png`, seven screenshots under `media/pr/crowdstrike-connector/`, and `tools/crowdstrike/{docker-compose.yaml,mock_falcon.py,stub_llm_crowdstrike.py}` — `git show --stat 8e941c3d`
- A follow-up commit replaced the icon with the official logo — `git log` `ca5ab94e`
- PostHog is the only registered product-analytics entry and sits in `category="services"` — `backend/app/schemas/data_source_registry.py:1701-1719`
- PostHog models `sessions` and `session_replay_events` as tables — `backend/app/data_sources/clients/posthog_client.py:66-89`, `:132-148`
- The HubSpot design doc records that choosing the JSON-spec archetype before verifying the vendor's real query surface was a mistake that had to be re-scoped — `docs/design/hubspot-connector.md` (superseded notice)

### Inferences
**Why CrowdStrike over the alternatives (assessment)**
- It is REST-only with no vendor SQL, which is the stated nature of Glassbox in the brief; it solves the three problems such a connector has (schema with no introspection endpoint, heterogeneous pagination, rate limits) in one file.
- AppDynamics is a close second for time-windowed metric queries (`duration_in_mins` / start-end parameters, `backend/app/data_sources/clients/appdynamics_client.py:462-468`), which resembles analytics reporting APIs; but it does not retry 429s.
- Google Analytics is the nearest domain match but a poor template: keyword-argument `execute_query`, hard-coded report tables, a vendor SDK instead of `pinned_session`, and no registry entry found.
- `custom_api` and MCP presets expose tools, not an indexed table catalog; per the HubSpot doc, a native `data_shape="tables"` connector is what gives indexed catalog and tracked queries. If Glassbox ships an official MCP server, a preset would be the cheap alternative (one `McpPreset` plus an icon mapping and a `test_mcp_presets.py` assertion).

**File checklist for a native Glassbox connector (assessment, derived from the skill plus commit `8e941c3d` and the enforcement tests)**

Add:
1. `backend/app/data_sources/clients/glassbox_client.py` — `GlassboxClient(DataSourceClient)` with `description`, `test_connection`, `get_schemas`, `get_schema`, `prompt_schema`, `execute_query`, `system_prompt`; sessions from `app.core.egress.pinned_session`.
2. `backend/tests/unit/test_glassbox_client.py` — fake only the `requests` boundary with an in-test scripted API.
3. `frontend/public/data_sources_icons/glassbox.png` (PNG avoids any Vue edit).
4. `docs/feedback-loops/glassbox-connector.md`.
5. `tools/glassbox/` simulator (`mock_*.py`, `docker-compose.yaml`), if no real tenant is available for CI or the sandbox loop.
6. `media/pr/glassbox-connector/*.png` evidence screenshots (connect form, tables list).
7. Optional `docs/design/glassbox-connector.md`.

Edit:
8. `backend/app/schemas/data_sources/configs.py` — `GlassboxConfig` plus one credentials class per auth variant.
9. `backend/app/schemas/data_source_registry.py` — imports plus a `"glassbox"` `REGISTRY` entry (`category="services"`, explicit `client_path`, `version="beta"`, optional `setup_guide`, `dev_only=True` while incubating).
10. `backend/tests/unit/test_connector_egress.py` — a `CASES` row.
11. `backend/tests/unit/test_query_params.py` — `"GlassboxClient"` in `_EXPECTED_LITERAL_RULES`.
12. `backend/tests/integrations/ds_clients.py` — `"glassbox"` in `DATA_SOURCES` with a remote-mode comment.
13. `README.md` connector table; `CHANGELOG.md` and `VERSION` (release-notes skill).
14. Only if needed: `frontend/components/DataSourceIcon.vue` (`TYPE_ICON_FILE`, for an SVG icon); `locales/*.json` `data.setupGuides.glassbox` in all ten catalogs (for a translated setup guide); `backend/pyproject.toml` + `backend/uv.lock` (only if a new dependency is introduced; `requests` and `pandas` are already used by existing clients).

Not expected to change: anything under `backend/app/ai/`, routes, services, Vue form components, Alembic migrations (CrowdStrike's commit touched none of them).

**Design decisions the implementer must settle first (assessment)**
- Whether Glassbox has a query language or only resource/report endpoints; this picks PostHog-style passthrough versus CrowdStrike-style JSON spec.
- Auth variant(s) and whether `"user"` scope makes sense; SaaS precedents with API keys use `scopes=["system","user"]` (PostHog, CrowdStrike), while Salesforce and NetSuite are `["system"]` only.
- Whether the API host is tenant-specific; if so name the config field `base_url` (or another recognised URL key) for the egress check.
- A non-empty virtual-table catalog is required for the integration harness's table-count assertion.

### Gaps
- Glassbox's actual API surface (endpoints, auth scheme, pagination, rate limits, any export/query language, existence of an MCP server) is not in this repo and was not researched here; every Glassbox-specific design choice above is conditional on it.
- The checklist was not validated by running the verification commands; items 10 and 11 are inferred from the tests' own docstrings rather than from an observed failure.
- Whether the PR description template (`.claude/templates/PR_DESCRIPTION_STANDARD.md`) imposes further connector-specific artefacts was not checked.
