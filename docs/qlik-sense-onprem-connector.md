# Qlik Sense Enterprise on Windows (on-prem) connector

Bag of Words could read Qlik in two shapes — `.qvd` files and Qlik **Cloud** —
and neither reaches a Qlik Sense Enterprise on Windows (QSEoW) site. The two
Qlik products share the Engine protocol and nothing else: discovery, auth, the
grouping concept and the WebSocket endpoint all differ, so the on-prem path is a
separate connector (`qlik_sense_onprem`) rather than a mode of `qlik_sense` —
the same split the repo already makes between `powerbi` and
`powerbi_report_server`.

|            | Qlik Cloud                | Qlik Sense Enterprise on Windows |
| ---------- | ------------------------- | -------------------------------- |
| Discovery  | REST `/api/v1/items`      | QRS REST on `:4242`              |
| Auth       | Bearer token              | mutual TLS (client certificate)  |
| Grouping   | Space                     | **Stream**                       |
| Engine     | `wss://tenant/app/{id}`   | `wss://host:4747/app/{id}`       |
| Identity   | the token's own user      | `X-Qlik-User` impersonation      |

## Evidence

Everything below was taken from a real QSEoW **31.62.0.0** site (4 streams,
7 apps) with a metadata dump run on the server itself. The redacted payload
shapes are the fixtures in
`backend/tests/unit/test_qlik_sense_onprem_client.py`; the raw dump is not in
the repo because it contained live credentials (see *Security* below).

## Protocol facts that are easy to get wrong

1. **`xrfkey` goes in two places.** QRS requires a 16-character nonce in the
   query string *and* in the `X-Qlik-Xrfkey` header, with identical values.
   Send one without the other and QRS answers HTTP 400 with a body that never
   mentions xrfkey. Pinned by
   `TestQrsRequestContract::test_xrfkey_is_sent_in_both_the_query_string_and_the_header`.

2. **The certificate is a service identity, not a person.** A QMC-exported
   client certificate authenticates *the machine*, so every request must also
   name the account Qlik should evaluate it as via
   `X-Qlik-User: UserDirectory=…; UserId=…`. That header is what decides which
   apps are visible and which Section Access rules apply — it is the row-level
   security story.

   Per-user auth builds on this with an **identity overlay**: the certificate
   variant is system-scope only (it is admin-equivalent on the site, so it must
   never be requested from end users), and the user-scope variant asks for just
   `User Directory` + `User ID`. The variant is marked `overlay=True` in the
   registry, and every credential resolver merges the user's identity fields
   over the connection's system credentials
   (`overlay_system_credentials` in `data_source_registry.py`) before the
   client is constructed — so the user's queries carry the admin's certificate
   but *their* `X-Qlik-User`, and Qlik applies their stream access and Section
   Access. The same mechanism is generic: any connector whose shared secret is
   heavy or admin-equivalent can add an overlay variant.

3. **Ports are fixed and separate.** QRS is `:4242`, the Engine is `:4747`, and
   neither is the QMC/hub port. Any port in the configured Server URL is
   discarded (`_parse_host`) so a pasted `https://host/qmc` cannot send QRS
   calls to 443.

4. **The Engine interleaves unsolicited frames.** `OnConnected` arrives before
   any reply and change events arrive between replies, so the JSON-RPC loop
   matches on `id` rather than taking the next frame. This is shared with the
   Cloud client in `_qix_common.QixSession`.

## What the crawl extracts

Per app, in one Engine session: `GetTablesAndKeys` (the data model),
`qMeasureListDef` / `qDimensionListDef` / `qVariableListDef` (master items),
`qAppObjectListDef` (sheets — the on-prem equivalent of the dashboards the
Power BI connector records), `GetLineage` and `GetScript`. Tables are named
`Stream/App/Table`.

Three findings from the real dump shaped the mapping:

- **Master measures carry their real expressions** —
  `Sum({$<[Region Name]={"Northeast"}>} [Sales Quantity]*[Sales Price])`. This
  is the genuine advantage over Power BI, whose `INFO.VIEW.MEASURES` often
  returns nothing usable. They are app-level, not table-level (a Qlik measure
  is an expression over any field), so they land in one synthetic
  `Stream/App/Master Items` entry per app rather than being duplicated onto
  every table.

  The expression has to go in the column's **`description`**. Column metadata
  is filtered by `_COLUMN_META_KEYS` in `tables_schema_section` on its way into
  the prompt, and `expression` is not on that allowlist — so a measure that
  carried its expression only in metadata would be extracted, stored, and never
  seen by the model. (This is why the Power BI client puts its DAX in
  `description` too.)

  Rendering those expressions also exposed a bug in shared code: `xml_escape`
  (`app/ai/context/sections/base.py`) escaped `&`, `<` and `>` but not `"`,
  while every value it produces goes into a double-quoted attribute. A measure
  using set analysis — `{$<[Region]={"North"}>}`, the form the real dump uses —
  therefore closed its own `description="…"` early and the rest was emitted as
  stray attributes. Fixed for every connector, not just this one: any column
  description or SQL comment containing a quote was equally affected.

  `TestMasterItemsReachThePrompt` renders the real context
  and asserts the expression survives, because a test against the built `Table`
  alone passes while the agent sees nothing.
- **An app can open successfully and have no data model.** `Content Monitor`
  returned `{"qtr": [], "qk": []}` and no `qHasData` — its script has never
  run — while carrying 100+ master measures. That is a *state*, not an error:
  the connector still publishes the master items, and only when there is
  nothing at all does it record an inactive row with `status: empty`. It never
  invents a column-less table.
- **Key fields fan out.** `Order Number` links five tables in one app. Since
  Qlik associations have no direction, each link is emitted from both sides —
  but past `_MAX_KEY_FANOUT` linked tables the edges are replaced by an
  `associativeHubKeys` note, because n×(n−1) edges say nothing a single note
  doesn't.

## What lands in `metadata_json`

The prompt allowlists what it renders (`_TABLE_META_KEYS` / `_COLUMN_META_KEYS`),
so storing metadata costs nothing at inference time — everything captured is
kept, and the renderer decides what the model sees. The shape follows the Power
BI connector: the app-level block is stamped on **every** table of the app, the
way `powerbi` stamps `datasetId` / `workspaceName` / `configuredBy` / `webUrl` /
`reports` / `rowLevelSecurity` on each of its own.

| | |
| --- | --- |
| **App** (on every table) | `appId`, `appName`, `appDescription`, `streamId`, `streamName`, `streamTags`, `streamCustomProperties`, `published`, `publishTime`, `lastReload`, `createdDate`, `modifiedDate`, `modifiedBy`, `owner`, `fileSizeBytes`, `availabilityStatus`, `tags`, `customProperties`, `webUrl`, `hasData`, `hasScript`, `engineFileName`, `alternateStates`, `scriptChars`, `sectionAccess`, `lineageSources`, `sheets` |
| **Table** | `tableName`, `rowCount`, `fieldCount`, `comment`, `position`, `associations`, `associativeHubKeys`, `loose`, `synthetic` |
| **Field** (column metadata) | `qlik_tags`, `rows`, `nonNulls`, `distinctValues`, `presentDistinctValues`, `informationDensity`, `subsetRatio`, `keyType`, `comment`, `derivedFields`, `synthetic`, `detail`, `semantic`, `onTheFly`, `hidden`, `relationship_key` |
| **Master Items row only** | `variables`, `sheetDetails`, `lineage` (with statements), `scriptExcerpt`, `measureCount`, `dimensionCount` |
| **Master item** (column metadata) | `expression`, `label`, `grouping`, `drilldown`, `itemId`, `tags`, `owner`, `created`, `modified`, `approved` |

Three notes on the choices:

- **Field statistics are the richest column metadata any BI connector here
  gets.** Qlik profiles every field at reload, so cardinality, null density and
  a key's `subsetRatio` come for free — answering "is this column selective
  enough to group by" without querying the app.
- **Two levels of detail for the same thing.** `sheets` and `lineageSources` are
  name-only lists cheap enough to repeat on every table; `sheetDetails` and the
  full `lineage` with SQL statements live once, on the Master Items row.
- **`sectionAccess`** is the Qlik equivalent of Power BI's `rowLevelSecurity`
  flag. It is declared only in the load script, so the script is the one place
  it can be detected — and it explains why two users get different numbers from
  the same app.
- **Master-item tags are the approval trail** on a governed site ("Certified",
  "Deprecated") — the difference between a measure the agent should reach for
  and one it should not.

## Security

- **`/qrs/dataconnection/full` returns credentials in the clear.** The live dump
  contained a real domain administrator password, repeated across nine REST
  connections. `list_data_connections()` therefore returns only id, name, type
  and architecture — the connection string, username and password never leave
  the method. Pinned by `TestDataConnections::test_credentials_never_leave_the_client`.
- **Lineage and variables are free text from the customer's load script** and
  can embed `Password=…` or an API key in a URL. Both are run through
  `_mask_secrets` before they reach `metadata_json`.
- **A Qlik client certificate is admin-equivalent** — it can act as any user on
  the site. The PEM blobs are stored in the encrypted credential store, written
  to a `0600` file in a `0700` temp directory only for as long as the client
  lives (`ssl.load_cert_chain` and `requests` both accept paths only), and
  shredded in `close()`.

## Reproducing

```bash
cd backend
BOW_DATABASE_URL="sqlite:///db/app.db" uv run pytest \
  tests/unit/test_qlik_sense_onprem_client.py -q
```

All QRS and Engine I/O is mocked; the certificate tests generate a throwaway
self-signed EC pair so `load_cert_chain` has something real to load.

## Form notes

- The three PEM fields (`client_cert`, `client_key`, `root_ca`) are
  `ui:type: textarea`, not `password`: a password input is single-line and
  strips newlines on paste, which silently corrupts a PEM (OpenSSL needs the
  line structure). BigQuery's service-account JSON is the precedent.
- The config form is deliberately slim (server URL, Verify SSL, stream filter,
  published-only, and the two ports). Crawl-behavior knobs
  (`impersonate_app_owner`, `include_master_items`, `include_lineage`,
  `max_apps`, `max_concurrency`, `timeout_sec`) remain client constructor
  parameters with defaults — they tune discovery internals, not connection
  identity, and the peer connectors (Power BI: one config field) set the bar.

## Not yet covered

- **JWT via a virtual proxy.** Certificates require reaching `:4242`/`:4747`
  directly. A JWT virtual proxy would let a deployment work over 443 only, but
  QSEoW from November 2024 onward requires a two-phase CSWSH handshake for
  those WebSockets (`GET /<vproxy>/qps/csrftoken` with the bearer, then open the
  socket with the returned cookie + `qlik-csrf-token` and no `Authorization`
  header, from an `Origin` in the QMC allow list). Deliberately left out rather
  than shipped untested.
- **Live end-to-end verification.** The unit suite pins the request contracts
  and the mapping against real payload shapes, but the connector has not yet
  been run against a live site from inside the product.
- **Bookmarks**, incremental re-crawl against a prior catalog, and using
  `lastReloadTime` to skip apps that have not changed. (Sheets *are* extracted;
  their per-visualisation `cells` layout is deliberately not.)
- **Whole-table reads.** `execute_query` builds a hypercube, so it needs at
  least one dimension or measure — "give me every row of this table" has to be
  written as a cube listing the fields. Same shape as the Qlik Cloud client.
