# Feedback Loop — Power BI OBO with row-level security (item-level model access)

Live validation against the real `bow14.onmicrosoft.com` tenant, run 2026-07-27
in a cloud sandbox. Triggered by a customer report:

> "The end user shouldn't have permission to the workspace, only the service
> account — but without workspace permissions there's no access to query the
> semantic layer. Users are only in the RLS permission groups, not the Fabric
> workspace, and then they can only reach the semantic layer."

The customer is describing the **correct** RLS topology, and the connector
could not express it. Everything below was measured, not inferred.

## Why this topology is forced on RLS customers

Microsoft's rules make the customer's setup the only one that works:

- RLS applies **only to workspace Viewers**. Admin/Member/Contributor have edit
  permission on the model and therefore **bypass RLS entirely**. So an
  RLS-protected model *requires* keeping end users out of the workspace.
- `executeQueries` needs **Read + Build** on the model. Read alone is not
  enough — that is the `Read` vs `ReadExplore` distinction in the API's own
  access-right enum.
- Granting Build does **not** weaken RLS: "Even if Viewers are given Build
  permissions to the semantic model, RLS still applies."

## What the tenant actually answers

Measured directly against api.powerbi.com with three real identities. Fixtures:
`rls_sales` (import + RLS roles, workspace `verify-rls`), `shared_orders`
(import, no RLS, workspace `obo-itemshare`), plus four pre-existing non-RLS
models.

| Identity | `GET /groups` sees ws | group-scoped executeQueries | tenant-level executeQueries | COLUMNSTATISTICS |
|---|---|---|---|---|
| Service principal (ws **Member**), non-RLS model | yes | **200** | 200 | 200 |
| Service principal (ws **Member**), **RLS** model | yes | **401** | 401 | 401 |
| demo1 (ws **Member**), RLS model | yes | 200 (6 rows, **RLS bypassed**) | 200 | 200 |
| demo2 (**Build only, no ws role**), non-RLS model | **no** | **401** | **200 (6 rows)** | 200 |
| demo2 (**Build only, no RLS role**), RLS model | no | 401 | **401 `RLSNotAuthorizedForModel`** | 401 |
| demo1 (**no access at all**) | no | 401 | **404 `PowerBIEntityNotFound`** | 404 |

Four findings, each load-bearing:

1. **A service principal cannot touch an RLS model at all** — 401 even as a
   workspace Member, while every non-RLS model in the same tenant answers 200.
   So for a customer whose models are RLS-protected, SP indexing yields
   **nothing**, and the shared catalog can only ever be seeded by a user
   identity. (Matches the documented limitation: "Service principals can't be
   added to an RLS role… RLS isn't applied for apps using a service principal
   as the final effective identity.")
2. **Item-level access is invisible to both listings.** `GET /groups` returns
   only workspaces the identity holds a *role* in, and `GET /myorg/datasets` is
   My-workspace-only (verified: it returned `[]` / only the user's own items).
   There is no delegated "list models shared with me" API.
3. **The workspace-scoped endpoint is the wrong one for these users** — 401
   where the tenant-level endpoint returns 200 for the same user and query.
4. **Build alone is not sufficient on an RLS model**: without RLS *role*
   membership the API returns `RLSNotAuthorizedForModel`. Users need Build
   **and** role membership. `404` (no access) vs `401` (access, wrong role) is a
   clean discriminator.

## What the product did before the fix

Reproduced end-to-end through the real product: connection created in the UI
with the master SP as system credentials and "Require user authentication" on,
then per-user OBO sign-in driven through the actual callback path
(`tools/agent/e2e_powerbi_obo_rls.py`).

| Step | Before |
|---|---|
| SP indexing | 7 model tables; both RLS models correctly reported in `unreadable_datasets` but excluded from the catalog |
| demo1 overlay (ws Member) | 7 tables incl. `rls_sales/Sales` (contributed via the existing union path) |
| **demo2 overlay (Build only)** | **6 tables — `shared_orders/Orders` missing**, though demo2 can query it |
| demo2 querying it | would 401: `_execute_dax_internal` always used the workspace-scoped URL |

The overlay is built by re-crawling the tenant with the user's token, which
starts at `GET /groups` — so a model reachable only item-level can never enter
that user's catalog.

## The fix

Three changes, one new primitive (probe-a-dataset-as-this-identity) used in
three places. `backend/app/data_sources/clients/powerbi_client.py`:

1. **Query routing** — `_execute_dax_internal` tries the workspace-scoped URL,
   and on 401/403 retries tenant-level. Success is remembered per workspace
   (`_tenant_scoped_workspaces`), so the extra request is paid once per
   workspace per client, never per query. A workspace member never falls back.
2. **Discovery** — `_probe_unlisted_prior_datasets` takes the datasets already
   in the catalog that this identity's listing did *not* return, probes each
   with `EVALUATE ROW("t",1)` tenant-level, and admits the ones that answer.
   Only known datasets are probed (no crawling), capped at
   `MAX_UNLISTED_PROBES = 60` because `executeQueries` is limited to ~120
   requests/min/user and that budget is shared with the user's real queries; the
   cap is logged, never silent.
3. **Connect gate** — `_probe_known_catalog` checks the indexed catalog before
   failing a user with no workspace role. This matters more than it looks: a
   failed connection test **deletes the just-saved credential**
   (`routes/connection_oauth.py`), so previously such a user could not connect
   at all. Genuine failures now also return `connectivity: true` and name the
   real requirement (Build permission, plus RLS role membership on an RLS
   model) instead of the SP-oriented "make it a Member or Contributor" advice —
   which, on an RLS model, is advice that would silently disable RLS.

`ConnectionService.construct_client` now attaches the connection's indexed table
metadata (as `construct_clients` already did), so the connect gate has dataset
IDs to probe.

## Results after the fix

| Test case | Result |
|---|---|
| demo1 (ws Member) overlay | 7 tables, incl. `rls_sales/Sales`, **not** `shared_orders/Orders` |
| demo2 (Build only) overlay | **7 tables, incl. `shared_orders/Orders`** |
| demo2 executes DAX on the item-shared model | group-scoped 401 → tenant-level 200, **6 rows** |
| demo1 executes DAX on the RLS model | group-scoped 200, no fallback used |
| demo2 end-to-end report through the chat UI | "total Amount per Region" → **EMEA 4,600 / US 9,000**, matching the seeded rows exactly |
| demo1 asks for the same model (negative control) | agent replies "I don't see a table named `shared_orders/Orders`", lists only its own 6 tables — no leak, no mid-run 403 |
| Regression suite | `tests/unit/test_powerbi_item_level_access.py` — 12 tests |
| Pre-existing Power BI suites | 109 passed (2 unrelated `ms_fabric` OAuth-scope failures pre-date this branch) |

Screenshots: `assets/powerbi-obo-rls-*.png`.

## Round 2 — RLS roles assigned, filtering proven end to end

The roles could not be assigned programmatically (see below), so a human
assigned them in the portal: demo1 → `USOnly`, demo2 → `EMEAOnly`, and demo1 was
demoted from workspace Member to **Viewer** so RLS applies to them at all.
Final topology: demo1 = Viewer + Build + `USOnly`; demo2 = Build only (no
workspace role) + `EMEAOnly`.

| | `/groups` sees ws | group-scoped | tenant-level | rows returned |
|---|---|---|---|---|
| demo1 (Viewer + Build + `USOnly`) | yes | 200 | 200 | **3 rows, US only, 9 000** |
| demo2 (Build only + `EMEAOnly`) | **no** | 401 | **200** | **3 rows, EMEA only, 4 600** |

Disjoint slices of the same 6-row / 13 600 model, each filtered to that user's
role — through BOW's own client stack, not just raw REST. RLS works, and the
tenant-level fallback is what makes demo2's half possible at all.

Two findings from this round:

- **`COLUMNSTATISTICS` works fine for an RLS-restricted identity** (200, 4
  columns, tenant-level). The residual hole flagged in round 1 — "a model whose
  only readers are RLS Viewers can be contributed by nobody" — **does not
  exist**. Such users can introspect and contribute the model themselves.
- **Power BI caches user permissions.** After demoting demo1 Member → Viewer,
  they still saw all 6 rows (RLS not applied) until
  `POST /v1.0/myorg/RefreshUserPermissions` was called with their token; the
  very next query returned the correct 3. The Get Groups docs warn about this
  ("User permissions for workspaces take time to get updated"). Anyone testing
  a permission change — us or a customer — will otherwise measure stale
  behavior and conclude RLS is broken. **Now handled** (see below).

## Round 3 — flush the permission cache on delegated crawls

The staleness runs *permissive*: a just-revoked user keeps reading rows they
should not until the cache catches up, over an unbounded window. So this is a
correctness issue, not just UX. `PowerBIClient.refresh_user_permissions()` now
issues the documented flush, wired into `get_schemas` and gated so it fires:

- **only for a delegated identity** — the service principal has no user
  permission cache; `_delegated` is set from whether a token was handed in at
  construction;
- **at most once per client** (`_perms_refreshed`); the effect is account-wide;
- **only on a crawl** — `get_schemas` is the catalog-build entry, reached on OBO
  sign-in and manual reload (the two moments access may just have changed) but
  NOT on the query path, which resolves dataset IDs from attached metadata and
  never calls `get_schemas`. So no `RefreshUserPermissions` is ever added to a
  hot query.

Best-effort: a failed flush never breaks discovery. The flush is asynchronous
on Microsoft's side — it reliably freshens the user's next queries (the
security-critical path) and usually the crawl in the same request.

Verified e2e against the live tenant:

| Path | RefreshUserPermissions fired? |
|---|---|
| demo2 OBO sign-in / overlay sync (delegated crawl) | **yes — HTTP 200**, then 8 tables |
| demo2 DAX query through BOW | **no** (0 calls), still 3 EMEA rows |
| service-principal reindex | **no** (0 calls), indexing completed |

Pinned by `tests/unit/test_powerbi_refresh_user_permissions.py` (flush-once,
SP-never, query-never, failure-safe, no-429-retry).

## Round 4 — full re-verification from a clean rebuild on `main`

After all three rounds merged, the whole stack was rebuilt from zero on `main`
(fresh DB, connection re-created through the UI, members re-invited) and every
leg re-run against the live tenant. All green:

| Check | Result |
|---|---|
| Power BI unit + e2e suites | **77 passed** |
| demo1 OBO sign-in (Viewer + Build + `USOnly`) | overlay = 7, incl `rls_sales/Sales`, flush fired |
| demo2 OBO sign-in (Build only + `EMEAOnly`) | overlay = 8, incl `rls_sales` + `shared_orders`, flush fired |
| demo1 query `rls_sales` through BOW | **3 rows, US only** (group-scoped) |
| demo2 query `rls_sales` through BOW | **3 rows, EMEA only** (tenant-level fallback) |
| demo2 query `shared_orders` (SP-visible) | 6 rows (tenant-level fallback) |
| per-user visibility (overlay == agent context) | demo1 = 7, demo2 = 8, admin = 8 canonical, no cross-leak |

Two things this round surfaced:

- **The inactive-contributed-model step is real and load-bearing.** On the fresh
  rebuild, `rls_sales` (user-contributed, so `is_active=False`) was NOT
  queryable until an admin activated it — `_attach_stored_table_metadata`
  filters `is_active=True`, and a no-workspace-role user's fallback crawl can't
  rediscover it without a prior catalog. Once activated, both users query it
  correctly. This is the documented "still open" item below, confirmed to bite
  exactly as described; it is not a regression.

- **Latency bug found and fixed: the flush must not retry on 429.**
  `RefreshUserPermissions` is aggressively rate-limited — back-to-back per-user
  syncs hit `429` with a ~30s `Retry-After`. The shared `_request` backoff loop
  then retried 3× (observed live: three 30s-spaced 429s), which would block the
  overlay sync — and thus interactive sign-in/reload — for up to a minute on a
  best-effort call. `refresh_user_permissions` now issues a SINGLE attempt
  (`max_attempts=1`); a 429 just means the cache was flushed recently, so it
  moves on. Pinned by `test_flush_does_not_retry_on_429`.

### Bug found and fixed in round 2

demo2 could SEE `rls_sales/Sales` in their catalog and had permission to query
it, but the agent could not execute against it:

    ValueError: Could not resolve Power BI dataset for table 'rls_sales/Sales'

`_attach_stored_table_metadata` supplies the dataset GUIDs the client needs, and
it INNER JOINed `ConnectionTable` — which drops every user-contributed row,
since those have no service-principal row to link to. The whole-catalog fallback
only fired when there were ZERO linked rows, so the bug is invisible in a pure
tenant and bites in a MIXED one (some models SP-visible, some not). Now an outer
join includes unlinked rows alongside this connection's own. Pinned by
`tests/e2e/test_powerbi_user_contributed_query_metadata.py` (fails without the
fix). `ConnectionService._attach_connection_table_metadata` gained the same
union, so the connect gate still has something to probe in a tenant where the SP
indexed nothing at all.

## Still open

- **RLS role membership cannot be automated.** It is a service-layer
  permission, not part of the model: TMDL silently drops a `member` block on
  create and rejects it on update (`Workload_FailedToParseFile`), and the
  Power BI portal is unreachable from the sandbox (Chromium gets
  `ERR_CONNECTION_RESET` against `app.powerbi.com` — the egress proxy rejects
  its TLS handshake, same as the prior OBO loop). Assigning roles is therefore a
  manual portal step: workspace item → **… → Security** → pick the role → add
  the member.
- **User-contributed catalog rows land inactive.** `_upsert_user_overlay`
  creates them with `is_active=False` for delegated connections, so a model
  only a user can see needs an admin to select it before it reaches the agent's
  context. In a tenant where *every* model is RLS-protected — i.e. where the SP
  indexes nothing — that means every model needs manual activation. Worth
  revisiting.

  Activation itself behaves correctly, measured with
  `tools/agent/e2e_powerbi_user_visibility.py` on `rls_sales/Sales` (a model the
  SP cannot see, contributed by demo1's own crawl):

  | | overlay | agent context |
  |---|---|---|
  | before activation, demo1 | has it | **absent** |
  | after activation, demo1 (can query it) | has it | **has it** |
  | after activation, demo2 (cannot query it) | absent | **absent** |

  So activation is a GATE, not a grant: it makes a model eligible, and each
  user still only receives it if their own token proved access. An admin cannot
  hand a model to a user who lacks permission on it.

  The catch is provenance: a contributed model's COLUMNS come from whoever
  contributed it. demo1 could produce them only because they are a workspace
  Member, where RLS is bypassed and `COLUMNSTATISTICS` works. Whether an
  RLS-restricted Viewer WITH role membership can run `COLUMNSTATISTICS` is
  **untested here** (role membership could not be assigned — see above). If it
  fails for them, a model whose only permitted readers are RLS Viewers can be
  contributed by nobody and stays invisible to everyone. That is the residual
  hole for a fully RLS-locked tenant, and it should be the next thing measured.
- **Tree vs list count mismatch** in the Tables view for user-contributed rows
  (left tree showed 6, right list 7). Cosmetic, pre-existing.

## Reproducing

Tenant fixtures created by this loop (kept for re-runs): workspace
`obo-itemshare` with model `shared_orders` shared to demo2 with `ReadExplore`
(Build) and no workspace role; model `rls_sales` in `verify-rls` with RLS roles
`EMEAOnly` / `USOnly`. Credentials come from env vars only.

```bash
# boot + seed (see the sandbox-feedback-loop skill)
BOW_DATABASE_URL=sqlite:///db/app.db BOW_ENCRYPTION_KEY=<fixed> uv run python main.py

# per-user OBO sign-in through the real callback path
BOW_RLS_LOCAL_EMAIL=demo2@example.com \
BOW_RLS_USER_EMAIL=demo2@… BOW_RLS_USER_PASSWORD=… \
  uv run python ../tools/agent/e2e_powerbi_obo_rls.py

# execute DAX as that member through the product's client stack
BOW_RLS_LOCAL_EMAIL=demo2@example.com BOW_RLS_TABLE='shared_orders/Orders' \
  uv run python ../tools/agent/e2e_powerbi_rls_query.py
```

A fixed `BOW_ENCRYPTION_KEY` is required: without it the server generates an
ephemeral key at boot and out-of-process harnesses cannot decrypt the
connection credentials it wrote.
