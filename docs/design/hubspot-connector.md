# Plan: HubSpot connector — `HubSpotClient`

## Mission

Add a data source type `hubspot` that lets the agent **query** a HubSpot portal
— contacts, companies, deals, tickets, engagements and custom objects — and get
rows back as a DataFrame, with `get_schemas` building tables from the portal's
own property definitions.

The original plan modelled `execute_query` on **ServiceNow** (a JSON query spec),
on the premise that HubSpot has no query language. That premise was wrong — see
the notice below before building anything.

This follows `.agents/skills/add-connection-type/SKILL.md`. A connector is
registry-driven: the frontend form, auth variants and client resolution all
derive from one entry in `backend/app/schemas/data_source_registry.py`.

All API facts below were taken from HubSpot's published OpenAPI specs
(`https://api.hubspot.com/public/api/spec/v1/specs` → per-API `openApi` URL),
not from documentation prose. Anything unverified is called out as such.

> ## ⚠️ Superseded in part by a live sign-in (2026-09)
>
> This plan was written believing **HubSpot has no query language**, and therefore
> modelled the connector on ServiceNow (a JSON filter spec) with aggregation done
> in pandas. A real portal sign-in disproved that premise.
>
> HubSpot's MCP server exposes **`query_crm_data` — CRM over SQL**, with
> aggregates, `GROUP BY`, `DATE_TRUNC`, `MEDIAN` and cross-object traversal.
> Verified: `SELECT COUNT(*) FROM CONTACT` → 1239. So HubSpot is far closer to
> **Salesforce/SOQL** than to ServiceNow, and the "aggregation ceiling" this doc
> treats as the defining constraint largely does not exist.
>
> Two things sharpen rather than remove the plan:
>
> * **SQL appears to be MCP-only.** `crm.hubsql.execute` is granted, but no public
>   REST endpoint was found (six paths probed, all 404) and HubSQL is in none of
>   HubSpot's 121 published API specs. A native connector cannot call it over REST
>   today — it would have to go through the MCP server, or fall back to the Search
>   API described below.
> * **One app and one token serve both surfaces** — the MCP-issued token works
>   against `api.hubapi.com` (`GET /crm/v3/properties/contacts` → 409 properties).
>   No second sign-in. This closes the open question in the Authentication section.
>
> Consequently the connector's value is no longer "make HubSpot queryable" — the
> shipped MCP preset already does that. It is the **indexed catalog** (409 contact
> properties in planner context), tracked queries and reports, and `data_shape=
> "tables"` integration. Re-scope before building. Details and evidence:
> `docs/feedback-loops/hubspot-mcp-preset.md`, Loop D.

## This is not the MCP preset

We already ship a HubSpot **MCP preset** (`docs/feedback-loops/hubspot-mcp-preset.md`).
It is a different archetype and the two are complementary, not alternatives:

| | MCP preset (shipped) | This connector (proposed) |
|---|---|---|
| `data_shape` | `tools` | `tables` |
| `catalog_ownership` | `none` | `shared` |
| `is_connection` | `False` | `True` (default) |
| Agent sees | whatever tools HubSpot's server exposes | an indexed catalog + `execute_query` |
| Good at | actions, and ad-hoc SQL via `query_crm_data` | the same, plus an indexed catalog and tracked reports |

An earlier version of this section claimed "the MCP tile cannot answer analytical
questions… no query surface, only fixed tool calls." That is **false**: one of its
tools is `query_crm_data`, CRM over SQL. What the tile genuinely lacks is a
*catalog* — the planner sees no schema, so it must discover properties by calling
tools mid-conversation instead of reasoning over 409 known columns up front. Gmail
still sets the precedent for the pairing — a native connector alongside an MCP
path.

## The query surface — Search API, not GraphQL

This was the open decision. Two candidates:

**`POST /crm/v3/objects/{objectType}/search`** — published, versioned, STABLE in
the spec catalog, and FREE on every HubSpot tier (`marketing`/`sales`/`service`/
`cms`/`commerce`/`crmHub`/`dataHub` all `FREE` for Contacts v3). Its request body
is already JSON, which matters below.

**`POST /collector/graphql`** — the route exists (it answers `401`, while a bogus
path under the same host answers `404`, so it is really routed). But it is **not
in HubSpot's published API spec catalog** — none of the 121 catalogued APIs is a
GraphQL endpoint. That makes it an unversioned, undocumented-by-contract surface.
It might well be nicer — a single round trip across associations is exactly what
the Search API lacks — but building the connector's only query path on an
uncatalogued endpoint is not a trade worth making.

**Decision: build on the CRM Search API.** Revisit GraphQL only if HubSpot
publishes it, and then only as an optimization behind the same query spec.

### What the Search API can and cannot do (verified from the spec)

`PublicObjectSearchRequest`:

| Field | Constraint (verbatim from the spec) |
|---|---|
| `filterGroups` | "Up to 6 groups of filters" — groups OR together, filters within a group AND together |
| `limit` | "The maximum results to return, up to 200 objects" |
| `after` | paging cursor token |
| `query` | free-text search, "up to 3000 characters" |
| `properties` | list of property names to return |
| `sorts` | sort by property |

`Filter.operator` is a closed enum of **13** values: `EQ`, `NEQ`, `LT`, `LTE`,
`GT`, `GTE`, `BETWEEN` (with `highValue`), `IN`, `NOT_IN` (with `values`),
`CONTAINS_TOKEN`, `NOT_CONTAINS_TOKEN`, `HAS_PROPERTY`, `NOT_HAS_PROPERTY`.

**There is no aggregation operator and no join.** This is the defining constraint
of the connector and drives everything below:

- **Aggregation happens in pandas**, after fetching rows. This is the exact
  inverse of the Salesforce client, whose system prompt pushes `GROUP BY` into
  SOQL specifically to avoid pulling rows. The HubSpot system prompt has to
  teach the opposite, and has to be honest that it has a ceiling.
- **Associations replace joins.** The search endpoint's own description says it
  supports "searching through associations", and Associations v4 is STABLE — but
  the shape of association filtering needs confirming against a live portal
  before the system prompt documents it.

## Data model: HubSpot objects → tables

`get_schemas()` returns one `Table` per object type, built from the portal's own
metadata rather than a hardcoded list — HubSpot portals accumulate large numbers
of custom properties, and those are precisely what the agent cannot see today.

- **Object types**: the standard set (`contacts`, `companies`, `deals`,
  `tickets`, `line_items`, `products`, `quotes`) plus engagements (`calls`,
  `emails`, `meetings`, `notes`, `tasks`), plus **custom objects** discovered
  from `GET /crm-object-schemas/v3/schemas`.
  (Note the host path: it is `/crm-object-schemas/v3/schemas`, *not*
  `/crm/v3/schemas`.)
- **Columns**: `GET /crm/v3/properties/{objectType}` returns `Property` objects
  carrying `name`, `label`, `description`, `type`, `fieldType`, `options`,
  `calculated`, `hidden`, `archived`, `referencedObjectType`. Map `name` →
  `TableColumn.name`, `type` → `dtype`, and `label`/`description` →
  `TableColumn.description` (the label is what a user would say out loud, so it
  materially helps the planner pick the right property).
- **Filtering**: skip `archived` properties. Whether to skip `hidden` ones is a
  judgement call — hidden means hidden in HubSpot's UI, not unusable via API.
- **`pks`**: `id` on every object.
- **`fks`**: HubSpot associations are many-to-many, so they do **not** map onto
  `ForeignKey` cleanly. Leave `fks` empty rather than fake it; expose
  associations through the query spec instead.

## Query spec (what `execute_query` accepts)

A JSON string, like ServiceNow's. The important difference: ServiceNow has to
invent a wrapper around an encoded query string, whereas **HubSpot's filter
language is already JSON**, so the spec should be a thin, mostly pass-through
envelope rather than a new DSL to learn and translate:

```json
{"object": "deals",
 "filterGroups": [{"filters": [
     {"propertyName": "dealstage", "operator": "EQ", "value": "closedwon"},
     {"propertyName": "closedate", "operator": "BETWEEN",
      "value": "2026-01-01", "highValue": "2026-03-31"}]}],
 "properties": ["dealname", "amount", "closedate", "hubspot_owner_id"],
 "sorts": ["closedate"],
 "limit": 1000}
```

The client owns what the API cannot express: it paginates `after` in pages of
200 up to a `MAX_ROWS` cap (Salesforce's client does the same at 10,000),
flattens the `{"id":…, "properties":{…}}` envelope into flat columns, and keeps
the requested `properties` as columns when zero rows match — so "no matching
records" stays distinguishable from "broken query", which is a lesson already
learned in `salesforce_client.py`.

`system_prompt()` is where most of the value lives (the Salesforce equivalent is
~100 lines). It must teach: the 13 operators and nothing else; groups OR /
filters AND; that aggregation is a pandas step, not a query feature; that
property names are internal names (`hubspot_owner_id`), not labels; and the row
ceiling.

## Authentication

**Not the same OAuth endpoints as the MCP preset.** An earlier draft of this doc
said "reuse the HubSpot app — same OAuth endpoints"; that is wrong. The MCP
preset and this connector talk to different hosts, and therefore different OAuth
endpoints:

| | MCP preset | This connector |
|---|---|---|
| authorize | `mcp.hubspot.com/oauth/authorize/user` | `app.hubspot.com/oauth/authorize` |
| token | `mcp.hubspot.com/oauth/v3/token` | `api.hubapi.com/oauth/v1/token` |
| resource | `mcp.hubspot.com` | `api.hubapi.com` |

The right-hand column is confirmed from HubSpot's published OAuth v1 spec
(`servers: [https://api.hubapi.com]`, `POST /oauth/v1/token`).

Whether the **app** can be shared is still open, and matters more than the
endpoints. The credential HubSpot issues for the MCP path comes from a distinct
*MCP Connector* app type (its own section in the developer UI, and
`client_credentials` on it returns `USER_LEVEL_CLIENT_CREDENTIALS_NOT_SUPPORTED`).
A conventional public app may be required here instead. Two things to settle
with a real token, in order:

1. Introspect an MCP-issued token at `GET /oauth/v1/access-tokens/{token}`
   (Oauth v1 spec). If it resolves — returning portal, app id and scopes — the
   two surfaces share a token family and one sign-in could serve both. If it
   401s, they are separate audiences and this connector needs its own OAuth leg,
   meaning users sign in twice.
2. If separate, decide whether that second sign-in is acceptable or whether the
   connector should default to a private app token instead.

A private-app **access token** variant is worth offering as the default for a
system-scoped connection regardless: a single pasted secret, no OAuth app to
register, and it sidesteps the question entirely.

`test_connection()` should call something cheap and universally available —
`GET /crm/v3/properties/contacts?limit=1` — rather than a search, so it does not
consume the search rate limit or depend on any records existing.

## The licensing decision

`_user_auth_needs_enterprise` (`backend/app/services/connection_service.py:42`)
gates on exactly one thing:

```python
return get_entry(conn_type).data_shape == "tables"
```

So a `data_shape="tables"` HubSpot connector with `auth_policy="user_required"`
is **Enterprise-gated**, while the MCP preset we shipped gets per-user OAuth for
free — same product, same HubSpot app, different tier, purely because one is
`tools` and one is `tables`.

The gate's docstring says the intent is "per-user identity / OBO into a
**database**", and a CRM is not what it had in mind. But `objects` is used in
this repo for document stores (Elasticsearch, MongoDB, OpenSearch), and every
SaaS-API queryable connector — Salesforce, ServiceNow, NetSuite, PostHog — is
`tables`. Miscategorising HubSpot as `objects` to dodge the gate would break that
convention and change agent-facing copy ("N objects" vs "N tables").

**Recommendation: ship `data_shape="tables"` with `scopes=["system"]` first** —
a system-credentialed connection, no per-user auth, no gate involved, no decision
forced. If per-user HubSpot is wanted later, that is a deliberate product call:
either it is an Enterprise feature, or the gate grows a carve-out. Decide it then,
with a real request behind it.

## Files to create / modify (in order, per the skill)

1. **Create** `backend/app/data_sources/clients/hubspot_client.py`
   - `class HubSpotClient(DataSourceClient)`, `capabilities = {Capability.QUERY}`.
   - `connect()` → `requests.Session` with the bearer token; a `_get`/`_post`
     helper that raises readable errors on 401/403/429 and surfaces HubSpot's
     error body, which is
     `{"status":"error","message":…,"correlationId":…,"category":…}` (mirroring
     `format_salesforce_error`). Keep the correlation id in the message — it is
     what HubSpot support asks for — and branch on `category`
     (`INVALID_AUTHENTICATION`, rate-limit, validation) rather than parsing the
     human-readable `message`.
   - `get_schemas()`, `get_schema()`, `execute_query()`, `prompt_schema()` via
     `ServiceFormatter`, `system_prompt()`, `test_connection()`, `description`.
   - Keep calls sync — the base provides the async wrappers.
2. **Edit** `backend/app/schemas/data_sources/configs.py`
   - `HubSpotConfig` — optional `objects` (comma-separated allow-list; empty =
     standard set + discovered custom objects), `include_hidden_properties: bool
     = False`, `max_rows: int`. Field titles/descriptions are user-facing form
     copy, not comments.
   - `HubSpotPrivateAppCredentials` — `access_token` (`ui:type: password`).
   - `HubSpotOAuthCredentials` — client id/secret, endpoints prefilled.
3. **Edit** `backend/app/schemas/data_source_registry.py` — a `"hubspot"` entry:
   `config_schema=HubSpotConfig`,
   `credentials_auth=AuthOptions(default="private_app", by_auth={...})`,
   **explicit** `client_path="app.data_sources.clients.hubspot_client.HubSpotClient"`,
   `category="services"`, `data_shape="tables"`, `catalog_ownership="shared"`,
   `ui_form="data_source"`, `dev_only=True` while incubating.
4. **Icon** — already present: `frontend/public/data_sources_icons/hubspot.png`
   (added with the MCP preset). `DataSourceIcon.vue` resolves
   `/data_sources_icons/<type>.png` from the type token, and `hubspot` is
   already in `CONNECTOR_ICON_FILE`, so **no frontend change**.
5. **Driver** — none. `requests` is already a dependency; no `pyproject.toml` or
   `Dockerfile` change.

Note that the type `hubspot` and the MCP preset key `hubspot` now coexist. They
do not collide (the preset resolves to `type="mcp"`), but both resolve to the
same icon, which is correct and intended.

## Verification

1. **Registry + import resolves**
   ```bash
   cd backend
   uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('hubspot'))"
   ```
2. **Unit** — `backend/tests/unit/test_hubspot_client.py`, mocking the HTTP
   boundary only (style of `tests/unit/test_druid_client.py`): `get_schemas()`
   builds the expected tables from a canned properties payload; `execute_query()`
   posts the right search body and flattens the response; pagination stops at
   `MAX_ROWS`; a zero-row result keeps its columns; errors surface the HubSpot
   message.
3. **Generic e2e still green**
   ```bash
   uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
   ```
4. **Integration** — add `"hubspot"` to `DATA_SOURCES` in
   `backend/tests/integrations/ds_clients.py`. There is no container for HubSpot,
   so this needs a developer-portal test account in `integrations.json` (local
   only; CI restores from `INTEGRATIONS_JSON_B64` — never commit it).
5. **Live UI pass** — create the connection → Test connection → the tables
   selector lists the objects → run a prompt that queries deals. Screenshots via
   the **ui-evidence** skill; the form is schema-generated, so this doubles as
   the review of the config schemas.
6. **Record the loop** as `docs/feedback-loops/hubspot-connector.md`.

## Pitfalls

- **Always set `client_path`.** `hubspot` → naive title-casing gives
  `HubspotClient`, but the class should be `HubSpotClient` (capital S) — this is
  exactly the rename-shaped bug the skill warns the dynamic fallback causes.
- **Rate limits are per-portal and search is stricter than the rest of the API.**
  Paginating a large result set at 200/page will hit them. The client needs
  backoff on 429 from the start, not after the first support ticket.
- **`properties` is opt-in.** A search that does not name properties returns
  only a thin default set — easy to misread as "the data is empty".
- **Property names ≠ labels.** `hubspot_owner_id` is an id, not a name;
  resolving owners to people needs the Owners API (`Crm Owners`, v3 STABLE).
- Do not let `is_connection` default get overridden — it must stay `True`, or
  schema indexing skips the type.

## Open questions

Resolved by the 2026-09 sign-in (evidence in the feedback-loop doc, Loop D):
~~one sign-in or two~~ — one app and one token serve both surfaces;
~~is there a query language~~ — yes, SQL via `query_crm_data`;
~~can the catalog be built from properties~~ — yes, 409 contact properties
returned over REST with the same token.

Still open:

- **The Search API result cap.** The 10,000 ceiling is documentation prose, not
  in the OpenAPI spec, and the test portal (1,239 contacts) was too small to
  reach it. Confirm before hardcoding `MAX_ROWS`, and keep it configurable.
- **HubSQL over REST.** `crm.hubsql.execute` is granted and SQL works through the
  MCP tool, but no public REST endpoint was found and HubSQL appears in none of
  the 121 published specs. If it stays MCP-only, a native connector's
  `execute_query` has to either call the MCP server or use the Search API — that
  is the main design fork left.
- **`/collector/graphql`** is real but scope-gated (403, "app hasn't been granted
  all required scopes"); capabilities still unassessed.
- **Association filtering** in the Search API — request shape unconfirmed.
- **Aggregation via Search.** Only relevant if the connector uses the Search API
  rather than SQL. If so, the escape hatches remain the **Exports API**
  (v3 STABLE) and the repo's `backend/app/data_sources/fast/` materialization
  layer, which turns "no aggregation API" into "aggregate locally in DuckDB"
  (non-SQL precedent: `fast/posthog_source.py`).
