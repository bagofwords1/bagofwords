# Cloudflare — Connector Design

**Status: design only. Nothing is implemented.** This doc answers "how should a
Cloudflare connector work?" — what Cloudflare exposes, which surface is worth
connecting, how it maps onto our registry-driven connector pattern, and what to
build in what order.

Date: 2026-09-04. Companion docs with the same shape:
`docs/aws-cloudwatch-connector-analysis.md`,
`docs/vmware-aria-storage-connectors-analysis.md`,
`docs/priority-erp-connector-analysis.md`.

---

## 0. Bottom line up front

1. **Build one native connector: `cloudflare`, over the GraphQL Analytics API**
   (`https://api.cloudflare.com/client/v4/graphql`). It is a single endpoint,
   a single auth story (API token, `Bearer`), and it fronts **70+ datasets**
   across zone and account scope — HTTP traffic, WAF/firewall events, Workers,
   DNS, Zero Trust, R2, Load Balancing. One connector covers all of them
   because the dataset list is *discoverable at runtime*, not hard-coded.
2. **The catalog is introspectable, which is the whole reason this works.**
   GraphQL `__schema` introspection gives the dataset nodes and their field
   sets; the `viewer { zones/accounts { settings } }` node gives each dataset's
   own limits (how far back it retains, max window per query, max fields, max
   records). So `get_schemas()` can build a real, per-deployment table catalog
   with accurate columns and per-table limits — no vendored dataset list to go
   stale. This is a better position than most of our infra connectors start in.
3. **Closest in-repo template: `prometheus_client.py`.** Same shape — an HTTP
   metrics API, not SQL; one "table" per logical series family; columns are
   dimensions plus aggregated value fields; `execute_query` takes the source's
   own query language and returns a flat DataFrame. Not `elasticsearch`/`splunk`
   (no free-text log search here) and not a SQL client.
4. **Logs are a separate problem and we already solve it.** Cloudflare has *no*
   log-search API — raw logs leave via **Logpush** into object storage. Logpush
   to **R2** is reachable today with **zero new code**: `S3Config.endpoint_url`
   already names Cloudflare R2 as a supported S3-compatible target
   (`backend/app/schemas/data_sources/configs.py:2463`). Document that as the
   log path; do not build log ingestion into the `cloudflare` connector.
5. **MCP presets are a pilot shortcut, not the answer.** Cloudflare runs ~17
   official remote MCP servers (`mcp.cloudflare.com`, `graphql.mcp…`,
   `observability.mcp…`, `radar.mcp…`, `logs.mcp…`, `auditlogs.mcp…`,
   `dns-analytics.mcp…`, …), all OAuth, all `data_shape="tools"`. A preset is
   ~10 lines and no client code, but tool output is prose — it does not produce
   the tabular DataFrames our charting and dashboard path needs, and there is no
   catalog for the planner to reason over. Ship a `cloudflare` MCP preset if a
   customer wants it *this week*; it does not replace the native connector.
6. **Two things must be designed in from the start, or the connector will
   mislead the agent:** every dataset node **requires a `limit` argument** (there
   is no "unlimited"), and the `*Adaptive*` datasets return **sampled** rows
   carrying a `sampleInterval` — a row is worth `sampleInterval` real events.
   Both belong in the client's `description` and in the query builder, not in a
   README nobody reads. See §5.

---

## 1. What Cloudflare actually exposes

| Surface | Endpoint | Shape | Verdict |
|---|---|---|---|
| **GraphQL Analytics API** | `POST /client/v4/graphql` | Aggregated + event-level analytics, 70+ datasets, zone & account scope | **Build this.** |
| REST API v4 | `/client/v4/...` (2,500+ endpoints) | Config/inventory (zones, DNS records, rules, Workers scripts) | Small slice only — a `zones` inventory table, so the agent can map a zone *name* to the `zoneTag` every GraphQL query needs. |
| Logpush | pushes to R2 / S3 / GCS / Splunk / … | Raw request/event logs | **Already covered** by the `s3` connector pointed at R2. |
| R2 | S3-compatible | Object storage | **Already covered** by `s3` + `endpoint_url`. |
| D1 | HTTP query API (SQLite) | Per-Worker application DB | Out of scope for v1 — a genuinely different connector, and rarely the analytics target. |
| Workers Analytics Engine | queried via a **SQL-ish HTTP API** | Custom Worker-emitted metrics | v2 candidate. Different query language; do not conflate with GraphQL in v1. |
| Radar | public internet-traffic data (also an MCP server) | Global, not customer-specific | Not a data source for a customer's own analytics. Skip. |

Everything a "how is my site doing / what is the WAF blocking / are my Workers
erroring" question needs is in the GraphQL API. That is the connector.

---

## 2. Auth

Two variants, mirroring how Cloudflare itself documents access:

- **`api_token` (default, recommended).** `Authorization: Bearer <token>`.
  Requires **Account · Account Analytics · Read** for account-scope datasets,
  and **Zone · Analytics · Read** on each zone in scope. Tokens are scopeable
  per-zone, expirable, and IP-restrictable — the right default.
- **`api_key` (legacy).** `X-Auth-Email` + `X-Auth-Key` global key. Offer it
  because some estates still run on it, but the field description should say
  plainly that the global key grants full-account access and a token is
  preferred.

Both variants get `scopes=["system", "user"]`: per-user tokens are meaningful
here (a Cloudflare token is naturally per-person and per-zone), so an org can
run this `user_required` and have each analyst see only their own zones.

---

## 3. Config + credentials schemas

These Pydantic classes **are** the connect form (`ConnectForm.vue` renders them
from `GET /available_data_sources`), so the `description` strings are product
copy, not comments.

```python
# backend/app/schemas/data_sources/configs.py

class CloudflareConfig(BaseModel):
    account_id: Optional[str]      # 32-hex account tag. Required for account-scope datasets.
    zone_tags: Optional[str]       # Comma-separated zone IDs. Blank = discover via REST /zones.
    api_base: str = "https://api.cloudflare.com/client/v4"   # override for gov/staging
    dataset_prefixes: Optional[str] # e.g. "httpRequests,firewall" — narrows catalog indexing
    default_lookback_days: int = 7  # window used when a query omits start/end
    max_rows: int = 10000           # hard cap injected as `limit` when a query omits one
    verify_ssl: bool = True
    timeout: int = 60

class CloudflareApiTokenCredentials(BaseModel):
    api_token: str                  # ui:type=password

class CloudflareApiKeyCredentials(BaseModel):
    email: str
    api_key: str                    # ui:type=password
```

Registry entry (`backend/app/schemas/data_source_registry.py`):

```python
"cloudflare": DataSourceRegistryEntry(
    type="cloudflare",
    category="infra",              # alongside prometheus / splunk / jaeger / aws_cost
    title="Cloudflare",
    description="Traffic, security and Workers analytics from Cloudflare's GraphQL "
                "Analytics API — HTTP requests, WAF events, DNS, Zero Trust and more, "
                "per zone or across the account.",
    config_schema=CloudflareConfig,
    credentials_auth=AuthOptions(default="api_token", by_auth={
        "api_token": AuthVariant(title="API Token", schema=CloudflareApiTokenCredentials,
                                 scopes=["system", "user"]),
        "api_key":   AuthVariant(title="Global API Key (legacy)", schema=CloudflareApiKeyCredentials,
                                 scopes=["system", "user"]),
    }),
    client_path="app.data_sources.clients.cloudflare_client.CloudflareClient",
    version="beta",
),
```

`data_shape` stays `"tables"`, `catalog_ownership` `"shared"`, `ui_form`
`"data_source"` — all defaults. Set `client_path` explicitly (the skill's first
listed pitfall). No new Python dependency: `requests` + `pandas` only.

---

## 4. Catalog — how `get_schemas()` builds tables

One **table per (scope, dataset)**, named `zone.<dataset>` /
`account.<dataset>`, e.g. `zone.httpRequestsAdaptiveGroups`,
`zone.firewallEventsAdaptive`, `account.workersInvocationsAdaptive`. Scope is in
the name because the same dataset often exists at both levels with different
retention, and the agent must not guess which one it is allowed to query.

Discovery, in three calls:

1. **`__schema` introspection** → the field lists of the `Zone` and `Account`
   nodes give the dataset names; each dataset's return type gives its fields.
2. **Field flattening** → `*Groups` datasets nest under `dimensions`, `sum`,
   `avg`, `count`, `quantiles`, `ratio`. Flatten to dotted column names so a
   table reads like a table: `dimensions.clientCountryName`,
   `sum.requests`, `avg.sampleInterval`, `quantiles.edgeTimeToFirstByteMsP95`.
   Non-`Groups` (event-level) datasets are already flat.
3. **`settings` node** → per-dataset limits (retention, max query window, max
   fields, max records). Store in `metadata_json` and render into the table
   `description`, so the planner sees *"7 days retention, ≤ 10,000 rows,
   ≤ 72h per query"* on the table itself rather than discovering it as a
   runtime error.

The dataset's GraphQL **filter input type** is also introspectable — capture the
legal filter keys into `metadata_json["filter_keys"]`. This is what stops the
agent inventing `WHERE country = ...` against an API that has no such thing.

Plus one non-analytics table:

- **`zones`** — from REST `GET /client/v4/zones`: `zone_tag`, `name`, `status`,
  `plan`, `account_id`. Without it the agent cannot turn "how did example.com
  do last week" into the `zoneTag` every GraphQL query requires.

Guardrails: `dataset_prefixes` narrows indexing; introspection is one HTTP call
so a full reindex is cheap; support `progress_callback` (the base inspects for
it) since flattening 70+ datasets is chatty in the UI even when it is fast.

---

## 5. Query contract

`execute_query` accepts **either** a raw GraphQL document (full power, for the
cases the builder cannot express) **or** a JSON spec (the ergonomic path the
agent will use 95% of the time):

```python
# JSON spec — the client assembles the GraphQL document
client.execute_query({
    "dataset": "zone.httpRequestsAdaptiveGroups",
    "zones": ["<zoneTag>"],                     # defaults to config zone_tags
    "since": "2026-09-01T00:00:00Z",            # -> filter.datetime_geq
    "until": "2026-09-04T00:00:00Z",            # -> filter.datetime_leq
    "filter": {"clientCountryName": "US", "edgeResponseStatus_gt": 499},
    "dimensions": ["clientCountryName", "edgeResponseStatus"],
    "metrics": ["sum.requests", "sum.bytes", "avg.sampleInterval"],
    "orderBy": ["sum_requests_DESC"],
    "limit": 500,
})

# Raw GraphQL — passed through untouched
client.execute_query('query { viewer { zones(filter:{zoneTag:"..."}) { ... } } }')
```

Both return a flat `pandas.DataFrame`: one row per group/event, dotted column
names matching the catalog. Nested `dimensions`/`sum`/`avg` objects are
flattened by the same helper `get_schemas()` uses, so **schema columns and
result columns are the same strings** — the single most common source of
"the agent wrote a query against columns that don't exist".

Non-negotiables the client must enforce, because the API will not forgive them:

- **`limit` is mandatory on every dataset node.** Inject `config.max_rows` when
  the spec omits it, clamped to the dataset's own `settings` maximum.
- **A time filter is mandatory in practice.** Inject
  `now - default_lookback_days` when the spec omits `since`.
- **Sampling.** `*Adaptive*` datasets return sampled rows with a
  `sampleInterval`; the honest estimate of a real count is
  `sum(sampleInterval)`, not `count(rows)`. The client should always include
  `avg(sampleInterval)`/`count` when the caller asks for a count, and say so in
  `description`.
- **GraphQL errors return HTTP 200** with an `errors[]` body. Raise on
  `errors[]` (Prometheus's `_api` does the equivalent for its
  `{"status":"error"}` envelope — mirror it) or every failure becomes an empty
  DataFrame and a confidently wrong answer.
- **Rate limit: 300 queries / 5 minutes.** Surface 429s as a typed, retryable
  error rather than a bare exception.
- **Zone fan-out caps at 10 zones per query**; account scope is one account.
  Chunk and concatenate if `zones` exceeds 10.

`relative_date_hint` (rendered next to `description` in every codegen prompt via
`render_ds_client_entry`, `backend/app/ai/prompt_formatters.py:6`):

> *"No relative-date functions — Cloudflare filters take absolute RFC3339
> instants. Compute the window in Python (`datetime.now(timezone.utc)`) and pass
> it as `since`/`until` in the query spec so saved reports re-resolve on every
> run."*

That hint is load-bearing: without it, generated Cloudflare queries freeze
literal dates and every scheduled dashboard silently goes stale.

`test_connection()` should run the cheapest real query — `viewer { zones { zoneTag } }`
— which exercises auth *and* the GraphQL engine, and report the number of
visible zones back in the success message.

---

## 6. What the agent is told (`description`)

Follow Prometheus: `description` = one line of instance identity + the full
usage guide. It is the only thing the coder agent sees about this connection.
It must carry, concretely:

- GraphQL, not SQL — no `SELECT`/`FROM`/`JOIN`.
- The spec-vs-raw-GraphQL contract, with 3–4 worked examples (top countries by
  requests; 5xx rate over time; WAF blocks by rule; Workers errors by script).
- Sampling and `sampleInterval`.
- `limit` is required; per-dataset caps live in each table's description.
- Zone tags come from the `zones` table, never from a guessed domain name.

---

## 7. Files to touch

| Layer | File | Change |
|---|---|---|
| Client | `backend/app/data_sources/clients/cloudflare_client.py` | New `CloudflareClient(DataSourceClient)` |
| Config | `backend/app/schemas/data_sources/configs.py` | `CloudflareConfig`, `CloudflareApiTokenCredentials`, `CloudflareApiKeyCredentials` |
| Registry | `backend/app/schemas/data_source_registry.py` | Import the three + add the `"cloudflare"` entry |
| Icon | `frontend/public/data_sources_icons/cloudflare.svg` | Brand mark + map in `DataSourceIcon.vue` (`TYPE_ICON_FILE`, since it is an SVG) |
| Tests | `backend/tests/unit/test_cloudflare_client.py` | Monkeypatched `requests.Session`, mirroring `test_prometheus_client.py` |
| Tests | `backend/tests/integrations/ds_clients.py` | Add `"cloudflare"` — remote mode, creds in `integrations.json` (never committed) |
| Docs | `docs/feedback-loops/cloudflare-connector.md` | The reproduce→fix→verify loop, per the **sandbox-feedback-loop** skill |

No frontend form work — the form is generated from the registry entry. No new
dependency.

---

## 8. Testing

- **Unit (no network).** Monkeypatch the session, as `test_prometheus_client.py`
  does. Cover: introspection → table set (scope prefixes, dotted column names);
  `settings` limits landing in `metadata_json`; spec → GraphQL document
  (injected `limit`, injected time window, filter-key mapping, >10-zone
  chunking); `dimensions`/`sum` flattening → DataFrame columns identical to
  catalog columns; `errors[]` in a 200 body raising; 429 surfacing as retryable;
  both auth variants setting the right headers; `resolve_client_class("cloudflare")`.
- **Integration.** No container exists — Cloudflare is SaaS-only. Remote mode
  against a real account with a read-only token, credentials in
  `integrations.json`. A free-plan zone is enough for
  `httpRequestsAdaptiveGroups`.
- **Live UI pass.** `tools/agent/boot_stack.sh` + `seed_org.py`; create the
  connection → Test connection → Tables Selector lists `zone.*`/`account.*`
  tables → prompt "top 10 countries by requests on <zone> last 7 days" and check
  the generated query carries a computed window and an explicit `limit`.
  Screenshots via the **ui-evidence** skill.

---

## 9. Build order

1. **v1 — `cloudflare` connector, zone scope.** Introspection catalog, JSON
   spec + raw GraphQL, `zones` table, API-token auth, `dev_only=True` while
   incubating. This is the connector-sized PR.
2. **v1.1** — account scope, legacy `api_key` auth, per-user token scoping,
   drop `dev_only`.
3. **v2 (only if asked)** — Workers Analytics Engine (its own query language),
   and an `mcp` preset for `graphql.mcp.cloudflare.com` / `observability` for
   customers who want tool-style access alongside the tabular connector.

Logpush→R2 needs no build: it is the existing `s3` connector with
`endpoint_url` set. Say so in the connector's docs page rather than absorbing
log search into this connector.

---

## 10. Open questions to settle during the build

- **Exact `settings` field names.** The settings node's per-dataset limit fields
  are documented as existing (retention, max duration, max fields, max records)
  but the precise names must be read off a live introspection before they are
  relied on. Fall back to config-level `max_rows` if a field is absent.
- **Catalog size.** 70+ datasets × 2 scopes is fine; if a large plan pushes that
  higher, `dataset_prefixes` is the escape hatch. Confirm against a real
  enterprise account before dropping `dev_only`.
- **Event-level datasets** (`firewallEventsAdaptive`) can return wide rows with
  deeply nested objects. Decide per-dataset whether to flatten fully or expose a
  JSON column; start with full flattening and revisit if a dataset explodes the
  column count.
- **Cross-zone aggregation.** Chunking at 10 zones gives correct per-zone rows
  but the agent must aggregate client-side. Confirm the description says that
  explicitly.

## Sources

- [Cloudflare GraphQL Analytics API](https://developers.cloudflare.com/analytics/graphql-api/)
- [Introspecting the schema](https://developers.cloudflare.com/analytics/graphql-api/features/discovery/introspection/)
- [API-token authentication](https://developers.cloudflare.com/analytics/graphql-api/getting-started/authentication/api-token-auth/)
- [GraphQL API limits](https://developers.cloudflare.com/analytics/graphql-api/limits/)
- [Cloudflare remote MCP servers](https://developers.cloudflare.com/agents/model-context-protocol/mcp-servers-for-cloudflare/)
