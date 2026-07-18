# Feedback Loop — Power BI: "not all the semantic models are selectable in schema, some are missing from the fetch"

Reported against a live Entra tenant (SP-seeded catalog + OBO users): some
Power BI semantic models that are visible in app.powerbi.com never show up as
selectable tables in the schema step, with no error anywhere. This loop
reproduces three independent silent-drop mechanisms, fixes each, and confirms
the discovery pipeline against the real tenant.

Status: **fixed.** The reproduction tests now assert the fixed contract and
pass (no more strict-xfail markers).

---

## Root cause (validated)

The selectable list is whatever `PowerBIClient.get_schemas()` emits into the
canonical catalog (plus, for OBO users, an overlay filter on top). A semantic
model could fall out at each stage:

1. **Admin-scan shadowing.** `_batch_admin_scan` records an entry for every
   dataset present in the scan result, including datasets the Scanner API
   returned **no schema** for (model not refreshed since enhanced-metadata
   scanning was enabled, DirectLake, all tables `isHidden`). `get_schemas()`
   used to treat "dataset id present in scan results" as final and **never
   attempt the COLUMNSTATISTICS fallback** for those datasets — even when it
   would have worked. The model vanished.

2. **Silent introspection failure.** Without admin-API rights every dataset
   relies on `EVALUATE COLUMNSTATISTICS()` via `executeQueries`, which needs
   **Build** permission and fails for RLS-protected / Viewer-only / DirectLake
   models. The failure was swallowed into `([], [])`, the REST `/tables`
   fallback only answers for Push datasets, and Phase 4 emits rows only by
   iterating discovered tables — so a dataset with zero tables produced **zero
   schema rows and no surfaced error** (a `logging.warning` was the only trace).

3. **Overlay ∩ canonical intersection (OBO).** For a `user_required` + oauth
   connection, a signed-in user's selector is scoped to overlay rows with
   `data_source_table_id IS NOT NULL`, but `_upsert_user_overlay` linked
   overlay rows to canonical `DataSourceTable` rows **by name only** and left
   `NULL` on a miss. Net effect: a model the user's own token can crawl but the
   service principal cannot (SP not a Member of that workspace, or dropped by
   1/2 above) was fetched into the overlay **and then filtered out of the
   selector**.

Additional scoping fact (API semantics, not loop-reproducible):
`list_workspaces` uses `GET /v1.0/myorg/groups`, which only returns workspaces
where the identity holds a workspace role — datasets shared directly and "My
Workspace" content never enter the crawl.

---

## The fix

All in `backend/app/data_sources/clients/powerbi_client.py`,
`backend/app/services/data_source_service.py`, and the indexing service.

1. **Fix 1 — stop admin-scan shadowing.** `get_schemas()` now accepts an
   admin-scan result as final only when it returned tables; an **empty** scan
   result falls through to COLUMNSTATISTICS instead of shadowing it.

2. **Fix 2 — report unreadable models, don't drop or fake them.**
   `get_dataset_tables_with_reason` / `_get_tables_via_column_stats_with_reason`
   return *why* introspection failed. A dataset that produced no tables is
   recorded in `PowerBIClient.discovery_diagnostics` and surfaced via
   `index_stats()` → the indexing runner writes `unreadable_datasets` (with
   reasons) into the job's `stats_json` and emits a `warn` event; `refresh_schema`
   logs a warning. No phantom column-less table is created (it would be
   unqueryable and just move the failure downstream).

3. **Fix 3 — union user-discovered tables into the canonical catalog.** For
   `user_required` connections, `_upsert_user_overlay` now creates the canonical
   `DataSourceTable` on demand from the user's crawl (tagged
   `discovered_by="user"`, matched by dataset/table identity, created inactive)
   and links the overlay to it. One org-level row backs the table, so usage /
   instructions aggregate across users; per-user visibility stays enforced by
   the overlay; SP re-index does not prune it (it prunes only
   ConnectionTable-linked rows, and these are intentionally unlinked). This
   reuses the on-demand pattern that per_user catalogs (OneDrive/Drive) already
   had; OneDrive's auto-activate behavior is preserved.

4. **Fix 4 — skip built-in system models.** `_is_system_dataset` skips Power
   BI's auto-created usage-metrics semantic models in discovery so they never
   pollute the catalog (nor count as "unreadable").

---

## Loop A — deterministic reproduction / fix verification (no external services)

All Power BI HTTP traffic is faked at the `requests.Session` boundary; every
line of client/service logic runs real. Overlay leg runs on SQLite.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
TESTING=true uv run pytest \
  tests/unit/test_powerbi_missing_models_repro.py \
  tests/e2e/test_powerbi_overlay_intersection_repro.py \
  -v -s
```

**Observed after the fix:**

```
Fix 1  test_admin_scan_entry_without_schema_must_not_shadow_fallback
       [fixed] emitted schema tables: ['CoveredModel/Sales', 'ShadowedModel/Facts']
       [fixed] COLUMNSTATISTICS attempted for d2: True

Fix 2  test_unintrospectable_dataset_reported_not_dropped_and_not_phantom
       [fixed] emitted schema tables: ['ReadableModel/Orders']
       [fixed] unreadable diagnostics: [('UnreadableModel', 'COLUMNSTATISTICS failed: not authorized ...')]
       (no phantom 'UnreadableModel/*' table; index_stats() reports it with a reason)

Fix 4  test_builtin_usage_metrics_models_are_skipped -> only ['RealModel/Sales'] emitted

Fix 3  test_user_visible_model_missing_from_sp_catalog_is_selectable
       [fixed] user1 selectable: ['ModelA/T1', 'ModelB/T2']    # ModelB was SP-invisible
       [fixed] canonical rows: {'ModelA/T1': (None, ...), 'ModelB/T2': ('user', <id>)}
       [fixed] both users' overlays link to the SAME ModelB canonical id  # usage aggregates
```

All tests pass. `tests/unit/test_powerbi_client.py` (36) and
`tests/e2e/test_obo_admin_catalog_before_signin.py` still pass.

## Loop B — live confirmation (real Entra tenant)

Secrets via env vars only (never committed). Uses the master service principal
and delegated ROPC tokens for the demo users.

```bash
export BOW_ENTRA_TENANT_ID=...            # tenant
export BOW_ENTRA_CLIENT_ID=... BOW_ENTRA_CLIENT_SECRET=...   # OAuth app (user sign-in)
export BOW_PBI_MASTER_CLIENT_ID=... BOW_PBI_MASTER_CLIENT_SECRET=...  # service principal
```

**Observed (2026-07-18):**

```
SP get_schemas(): 2 workspaces (BOW, bow-bi); 4 semantic models all discovered
  SalesPush -> [Sales, Customers]; deals2 -> [...]; leads -> [...]; mySM -> [Docum, D (2)]
  index_stats(): {}                       # every model readable → no false diagnostics
_batch_admin_scan(): {} (empty)           # read-only admin API NOT enabled for the SP
                                          #  → discovery runs entirely on COLUMNSTATISTICS
                                          #    (the Fix 1 fallthrough path is the active one)
Delegated ROPC tokens (demo1 AllFabric, demo2 MinimalFabric): both HTTP 200
  demo1 get_schemas(): 4 models   demo2 get_schemas(): 4 models
```

**What Loop B proves and what it doesn't.** This tenant's three identities (SP,
demo1, demo2) are all Members of the same workspace and every model is
import-mode, so all read cleanly and no divergence appears live — Loop B
confirms the happy path (auth, discovery, no false diagnostics, delegated
tokens) and does **not** by itself exercise the three drop mechanisms; Loop A
covers those deterministically. Crucially, the SP's admin scan being **empty**
(read-only admin API off) is exactly the configuration under which a customer's
**DirectLake** models fail COLUMNSTATISTICS — which is why the top operational
recommendation is to enable the two Fabric admin-portal settings ("Service
principals can access read-only admin APIs" + "Enhanced admin API responses
with detailed metadata"); with Fix 1 in place, a schema-less scan entry no
longer shadows the DAX fallback.

## What this proves / regression notes

- Datasets are **listed** correctly in every case — the loss was in table
  introspection and the overlay link step, matching the report.
- Pre-existing, unrelated failures (reproduce with this change stashed):
  `tests/unit/test_connection_oauth.py::test_ms_fabric` and
  `::test_obo_exchange_ms_fabric` assert the old `api.fabric.microsoft.com`
  OAuth scope — untouched by this change.
- Sibling loops: `fabric-powerbi-obo-creator-zero-tables.md`,
  `powerbi-dataset-id-resolution.md`.
