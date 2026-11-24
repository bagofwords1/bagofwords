# Feedback Loop — SharePoint Server lists as tables (`sharepoint_onprem_lists`)

Request: query SharePoint **lists** on an on-premises SharePoint Server farm
(NTLM/Kerberos, no Graph), the way `sharepoint_lists` does for SharePoint
Online. Before this change the only on-prem connector, `sharepoint_onprem`,
read document libraries only (`_sharepoint_rest.py` asks for `BaseType eq 1`),
and the lists connector, `sharepoint_lists`, spoke only Microsoft Graph.

The claim this loop validates: every list on a SharePoint Server site becomes a
table, and **any column can be filtered on a list of any size**, which is what a
naive port of the Graph connector would get wrong.

## Root cause / design constraint (validated)

SharePoint Server enforces the **List View Threshold** (5,000 items by default):
a query that has to scan more items than that is refused with
`SPQueryThrottledException`, however few rows it returns. A `$filter` or
`$orderby` on an unindexed column scans the whole list, and `$top` is applied
after the filter. Graph has a header to override this
(`graph_list_client.py`, `PREFER_NONINDEXED`); SharePoint Server has none.
Sources: Microsoft's [threshold article](https://support.microsoft.com/en-US/SharePoint/data-and-lists/working-with-the-list-view-threshold-limit-for-sharepoint-server-only),
[Tech Community](https://techcommunity.microsoft.com/t5/sharepoint-developer/rest-api-quot-the-attempted-operation-is-prohibited-because-it/m-p/3902802),
[PnP large-list guidance](https://pnp.github.io/community-docs/articles/large-lists-large-libraries-in-SharePoint.html).

So the connector never sends `$filter`/`$orderby`. It pages lists in ID order
(`$top` + `$skiptoken=Paged=TRUE&p_ID=<last id>`, threshold-safe at any size,
built from the last ID seen rather than a server-supplied link) and evaluates
the spec's OData filter and sort locally:

- `backend/app/data_sources/clients/sharepoint_onprem_lists_client.py` — catalog, paging, value normalization, notices.
- `backend/app/data_sources/clients/_odata_filter.py` — a small OData parser/evaluator (no `eval`).
- `backend/app/data_sources/clients/_sharepoint_list_spec.py` — spec parsing shared with `graph_list_client.py`.

Other SharePoint Server details it handles, each covered by a test: display ↔
internal ↔ `EntityPropertyName` names (`2017` → `_x0032_017` →
`OData__x0032_017`); person/lookup columns expanded to names (`Title`, never
`EMail`, which fails a query when one user lacks an email) and capped below the
lookup-join threshold, falling back to IDs with a notice; taxonomy labels read
from the hidden companion field (REST `Label` is the WssId); folders skipped;
rich text reduced to text; verbose and nometadata JSON; 429/503 retried with a
capped `Retry-After`; oversized pages retried at half size; redirects refused;
provider error text never surfaced.

## Loop A — deterministic (no external services)

```bash
cd backend
uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/unit/test_odata_filter.py tests/unit/test_sharepoint_onprem_lists_client.py \
  tests/unit/test_graph_list_client.py tests/unit/test_connector_egress.py -q
uv run pytest tests/e2e/test_sharepoint_onprem_lists.py tests/e2e/test_sharepoint_onprem.py \
  tests/e2e/test_data_source.py tests/e2e/test_connection.py -m e2e --db=sqlite -q
```

The SharePoint boundary is `tests/fixtures/fake_sharepoint_lists.py`, an
in-memory REST provider that **enforces the List View Threshold and the lookup
threshold** the way the server does (HTTP 500 `SPQueryThrottledException`),
plus `$top`/`$skiptoken`/`__next` paging, `$select`/`$expand` validation,
verbose/nometadata, per-identity list permissions, and a 12,000-item `Tickets`
list. Everything else (registry, routes, services, encrypted credentials, DB)
is real.

Observed:

| Step | Result |
|---|---|
| New tests on base code (registry change stashed) | `KeyError: 'sharepoint_onprem_lists'` |
| Member e2e with the manual-credential wiring removed from `connection_service.py` | 1 failed (member save refused with 400) |
| Mutation: send the filter to the server (Graph-style) | **10 failed** (threshold refusals on the 12,000-item list) |
| Mutation: case-sensitive text / keep folders / no lookup cap / no look-ahead / ignore expanded names | 4 / 1 / 1 / 1 / 4 failed |
| With the change | `test_odata_filter` 47 passed, client 46 passed, e2e 3 passed |
| Related unit suites (`-k "sharepoint or graph_list or discovery or data_source or connector or registry or odata or egress or catalog or available"`) | 3264 passed; 6 failures identical on base (see below) |
| Registry-wide contracts (`test_discovery_progress_contract`, `test_fast_sql_dialect`, `test_incremental_connectors`) with unixODBC installed | 99 passed |
| e2e data source + connection + both SharePoint on-prem suites | 41 passed |

The filter tests compare each result with the truth computed straight from the
fixture's items (status, priority ranges, `contains`, dates, person names,
combined with `ID`), and assert that no items request carried `$filter` or
`$orderby`.

## Loop B — live: real NTLM over TLS, real app, real UI

No SharePoint farm is available in the sandbox, so
`tools/agent/mock_sharepoint_lists_server.py` serves the same provider over
HTTPS behind a **genuine NTLM handshake with channel binding** (Extended
Protection), so the connector's real session, auth and REST code run end to end.

```bash
cd backend
uv run python ../tools/agent/mock_sharepoint_lists_server.py --port 8444 --tls \
  --require-channel-binding --cert-out /tmp/mock-sp-ca.pem --log /tmp/mock-sp.log
# Backend on its own SQLite DB with an in-process enterprise license
# (exchange_onprem_sandbox.py is generic: it only boots the stock app licensed):
cat "$REQUESTS_CA_BUNDLE" /tmp/mock-sp-ca.pem > /tmp/bundle.pem   # or the system bundle
REQUESTS_CA_BUNDLE=/tmp/bundle.pem BOW_ENCRYPTION_KEY=<fernet key> \
  BOW_SANDBOX_DB=/tmp/sp_lists_sandbox.db uv run python ../tools/agent/exchange_onprem_sandbox.py
(cd ../frontend && yarn install --frozen-lockfile && yarn dev)
uv run python ../tools/agent/seed_org.py
# A funded LLM key, or the scripted stub:
STUB_PORT=9099 uv run python ../tools/agent/stub_llm_sharepoint_lists.py
uv run python ../tools/agent/setup_stub_llm.py --stub-url http://127.0.0.1:9099/v1
# UI (copy the flow into frontend/ so @playwright/test resolves):
cp ../tools/agent/sharepoint_lists_ui_flow.mjs ../frontend/.sp_flow.mjs && cd ../frontend
SP_PASSWORD='Lab-Svc-1!' node .sp_flow.mjs connect ../media/pr/sharepoint-onprem-lists
node .sp_flow.mjs chat ../media/pr/sharepoint-onprem-lists \
  "How many open tickets with priority 4 or higher does each person have?" tickets 300
```

DevBox (`BOW_SANDBOX_DEVBOX=1`): skip the backend/frontend/seed lines (the
stack is running and seeded; `bow-stack restart backend` picks up the client)
and log in as the DevBox admin.

Observed:

| Check | Result |
|---|---|
| `noa` / `dana` / wrong password, direct client | 4 lists / 5 lists (incl. `Salaries`) / HTTP 401 mapped, no provider text |
| Sign-ins in the mock log | all `channel_bound: true`; the wrong password refused |
| Filtered + sorted query on the 12,000-item list over real NTLM | 3 rows, 0.6 s, 12 pages of 1,000 |
| Every items request in the log | `$select`/`$expand`/`$top`/`$skiptoken` only — **no `$filter`, no `$orderby`**; all HTTP 200 |
| UI: catalog search "sharepoint" | the new **SharePoint Server Lists (on-prem)** tile next to the existing three (`01`) |
| UI: schema-generated form → Connect | "Connection saved · Schema discovery · Discovered 5 lists" (`03`, `04`) |
| UI: table selection | Expenses, Tickets, Projects, Rota, Salaries (`06`) |
| UI: report turn | create_data → codegen → sandbox → connector → mock; answer **1,200**, equal to the truth computed from the fixture (`12`) |
| Codegen prompt | contains the connector's query-spec docs (`execute_query(query)`, "filter and sort run after the rows are read") |
| Standard integration harness against the mock (`SHAREPOINT_LISTS_TEST_*`) | 1 passed |

Screenshots: `media/pr/sharepoint-onprem-lists/`.

The sandbox's Anthropic key had no credit, so the report turn used the scripted
stub (`stub_llm_sharepoint_lists.py`), the same pattern as the Intune loop. The
stub fixes *what* is asked; the data path (planner tool call → create_data →
codegen → sandboxed `generate_df` → connector → NTLM → mock) is the product's.
A real model's choice of filter is **not** verified here.

## Against a real farm (Loop C, credentials required)

```bash
cd backend
SHAREPOINT_LISTS_TEST_SITE_URL=https://sharepoint.example.com/sites/ops \
SHAREPOINT_LISTS_TEST_USERNAME='CORP\reader' SHAREPOINT_LISTS_TEST_PASSWORD=... \
TESTING=true uv run pytest tests/integrations/ds_clients.py -k sharepoint_onprem_lists -q
```

Then, in the UI, filter a list with more than 5,000 items on an unindexed
column and check the count against the list view in SharePoint.

## Pre-existing failures (identical with the change stashed)

In this container: `test_egress::test_requests_tls_certificate_is_verified_against_the_hostname`
and several on-prem tests fail because the sandbox sets `REQUESTS_CA_BUNDLE`
(tests assert `session.verify is True`; run with it unset);
`test_obo_background_database` (2); and the registry-wide tests fail on
`libodbc.so.2` until `apt-get install unixodbc`.

## Boundaries / not verified

- **No real SharePoint farm.** Paging past the threshold with `$skiptoken`, the
  `SPQueryThrottledException` body, the lookup-threshold message and taxonomy
  companion fields follow Microsoft's documentation and community reports; the
  mock reproduces them but is not the server. Run Loop C before relying on it.
- SharePoint 2010 lists (SOAP `GetListItems`) are not implemented; 2013+ only.
- Live Kerberos is not verified (same as `sharepoint_onprem`).
- Filtering a list larger than **Max items per query** covers only the first
  items, with a notice on the result; no server-side filtering is attempted,
  even on small lists.
