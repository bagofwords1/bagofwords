# Feedback Loop — Qlik Sense Enterprise on Windows (on-prem) connector

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
   security story, and it is why the acting identity lives in the
   **credentials** schema (a user-scoped credential reuses the same certificate
   with that user's `UserId`).

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
`GetLineage` and `GetScript`. Tables are named `Stream/App/Table`.

Three findings from the real dump shaped the mapping:

- **Master measures carry their real expressions** —
  `Sum({$<[Region Name]={"Northeast"}>} [Sales Quantity]*[Sales Price])`. This
  is the genuine advantage over Power BI, whose `INFO.VIEW.MEASURES` often
  returns nothing usable. They are app-level, not table-level (a Qlik measure
  is an expression over any field), so they land in one synthetic
  `Stream/App/Master Items` entry per app rather than being duplicated onto
  every table.
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
- **Sheets and bookmarks**, incremental re-crawl against a prior catalog, and
  using `lastReloadTime` to skip apps that have not changed.
