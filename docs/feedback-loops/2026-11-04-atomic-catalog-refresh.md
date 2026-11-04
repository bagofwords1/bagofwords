# Feedback loop — agent table selections disappear after incomplete indexing

Implements a per-connection publication guarantee: incomplete discovery, a publication error, or cancellation before commit leaves the last successful catalog and agent selections unchanged. Requested after a reported overnight indexing incident. No deployment or historical-selection restoration was performed.

## Root cause, validated

The original `ConnectionService.refresh_schema` logged Power BI's unreadable-model diagnostics but accepted the remaining models' tables as a complete result. It deleted absent `ConnectionTable` rows; `ConnectionTable.domain_tables` has `cascade="all, delete-orphan"`, so linked `DataSourceTable` rows and activation flags disappeared too. Later rediscovery created inactive rows.

The old transaction boundaries also committed connection tables before syncing agents, and background indexing synchronized each agent in another transaction. A rollback after those commits could not restore the previous snapshot. Optional change notifications can commit and schedule work, so they must run after publication.

Code navigation:

- `backend/app/data_sources/clients/discovery.py:39`: shared completeness result, failure signal, and compatibility with existing unread-prefix/type/model diagnostics.
- `backend/app/data_sources/clients/base.py`: validate table/file discovery in the common async wrapper.
- `backend/app/data_sources/clients/tool_provider_base.py` and `mcp_client.py`: equivalent validation for tool catalogs; capped pagination is incomplete.
- `backend/app/services/connection_service.py:1648`, `refresh_schema`: one commit for catalog reconciliation, linked agents, and successful-sync metadata; explicit rollback on failure or cancellation.
- `backend/app/services/catalog_refresh_service.py`: serialize publication per connection after discovery finishes.
- `backend/app/services/data_source_service.py:5206`, `refresh_user_connection_catalog`: discover once per user/connection and publish all linked agent overlays together.
- `backend/app/services/connection_indexing_service.py`: remove separate agent commits; retain job failure and connection error together after rollback using a captured ID, rather than an expired ORM row.
- `backend/app/routes/connection.py`, personal refresh: propagate incomplete discovery instead of returning a false success.

## Loop A — deterministic, no credentials

Use Python 3.13 (3.12+ supported) and an isolated checkout of the change. Standard setup:

```bash
cd backend
uv sync --frozen --extra dev
BOW_DATABASE_URL=sqlite:///db/app.db uv run pytest \
  tests/e2e/test_catalog_refresh_atomicity.py \
  tests/unit/test_catalog_discovery_contract.py \
  --db=sqlite -q --tb=short
```

External connector calls are replaced at client/transport boundaries. Services, SQLAlchemy, real database transactions, models, and HTTP authorization run normally. The historical snapshot has two agents, selected and unselected tables, stable row IDs, and a previous successful-sync timestamp. Direct seeding is limited to that historical baseline and explained in the test.

Before implementation, the first reproduction failed on the unread-model case: the successful-sync timestamp changed and the catalog/selection snapshot differed. Running all initial signal variants gave **3 failed, 2 passed**: unreadable models, unread prefixes and unread entry types were incorrectly accepted; raised exceptions and cancellation already stopped discovery.

A later background regression exposed an additional failure: the job was marked failed, but `last_reindex_error` remained NULL because rollback expired the ORM row used to find the connection. The observed assertion was `assert None`. Capturing the connection ID and committing both failure records together fixed it.

The generalized regression set now verifies:

- Incomplete model, prefix, entry-type and explicit-result signals, missing snapshots, raised errors, cooperative cancellation and task cancellation preserve the entire previous snapshot.
- A database failure on the second agent rolls back the first agent and the connection catalog, even if the caller catches the exception and tries to commit.
- Cancellation between agent syncs and immediately before commit does the same.
- Complete discovery propagates schema changes to every linked agent, preserves surviving selected row IDs, inserts new tables inactive, and removes genuine deletions.
- A complete, explicitly attested empty catalog is valid; a legacy bare empty result cannot clear an existing catalog.
- Personal overlay discovery and database publication failures preserve every linked agent's prior tables/column-access state.
- Background failure retains the last successful timestamp/catalog and records failed status plus the connection error.
- Tool discovery rejects capped pagination, preserves cancellation, and permits a successful retry.
- Tool policy tests distinguish a complete empty result from an incomplete result and verify existing preferences survive incomplete discovery.
- Existing shared-catalog union and admin/member tests retain identity and authorization behavior.

### Database verification

The deterministic tests were run on SQLite and a local throwaway PostgreSQL 14.15 database. PostgreSQL was available locally; Docker was unavailable, so the existing-server mode was used:

```bash
TEST_DATABASE_URL=postgresql://localhost/bow_atomic_tests \
BOW_DATABASE_URL=sqlite:///db/app.db uv run pytest \
  tests/e2e/test_catalog_refresh_atomicity.py \
  tests/e2e/test_powerbi_refresh_identity.py \
  tests/e2e/test_shared_catalog_union_per_user.py \
  tests/e2e/test_connection_indexing.py \
  tests/e2e/test_connection_indexing_user_scope.py \
  tests/e2e/test_connection_auto_reindex.py \
  tests/e2e/rbac/test_rbac_tool_policies.py \
  --db=postgres -q --tb=short
```

Final core verification: **67 end-to-end cases passed on each database**; the combined SQLite run including the four new tool-discovery contract cases passed **71 tests**. The affected-client sweep passed **894 tests, 10 skipped** across 23 existing client/progress test files. The skips are existing optional-test conditions, not newly disabled tests. Locale key drift was unchanged across all ten catalogs.

Additional compatibility verification passed **28 SQLite cases** covering the new regressions plus catalog uniqueness, user-contributed metadata, overlay intersection and tokenless-owner reload. The corresponding PostgreSQL run, also including the Power BI identity suite, passed **49 cases**. A Graph/discovery-contract follow-up passed **94 cases**. In the user's working tree, the core check passed **71 cases**, followed by **18 focused cases** after the final boundary adjustment.

The compatibility sweep caught a new interaction: rolling back a read-only personal discovery failure expired the caller's loaded ORM records, breaking tokenless-owner fallback. The final wrapper rolls back only after overlay publication has started (or lets the enclosing connection transaction roll back), and Reload permits only the expected missing-credential fallback; other discovery/publication failures propagate. The same existing compatibility test changed from FAIL to PASS.

The existing Power BI personal-refresh tests previously expected unreadable discovery to succeed and clear the overlay. They now require failed status/HTTP 503 and preservation of the last snapshot, for both admins and members. This is the intended behavior change.

## Loop B — real Power BI, local disposable application database

The opt-in integration is `backend/tests/integrations/test_catalog_refresh_live_powerbi.py`. Supply credentials through environment variables only:

- `PBI_TENANT_ID`, `PBI_CLIENT_ID`, `PBI_CLIENT_SECRET`.
- Optional `PBI_ACCESS_TOKEN` for a delegated identity with a fully readable catalog.
- Optional `PBI_WORKSPACES` to constrain discovery to the connection's intended workspace scope.

```bash
cd backend
BOW_DATABASE_URL=sqlite:///db/app.db uv run pytest \
  tests/integrations/test_catalog_refresh_live_powerbi.py \
  --db=sqlite -q -s --tb=short
```

Live observations:

- Service-principal authentication succeeded, but two listed models returned HTTP 401 `PowerBINotAuthorizedException`. Complete discovery was rejected, so this identity could not establish the baseline needed for the recovery loop. No source grants were changed.
- One delegated test identity produced 63 readable tables and two unreadable models; it was also rejected as incomplete.
- Another delegated test identity produced a complete catalog of **51 tables**. The integration seeded two local agents, indexed that live catalog, and selected their tables.
- A transport fault was then injected only in the test process for one model's introspection calls (including its fallbacks). Other models continued to use the real Power BI API. The refresh failed and the local snapshot, selected table IDs, and columns remained identical.
- Removing the transport fault allowed another complete live refresh; selections and row identities were still unchanged.

Observed output:

```text
Live Power BI loop: 51 tables; injected model timeout retained catalog and selections; recovery retained row identities.
1 passed
```

The integration uses normal client discovery/DAX metadata requests, including its delegated permission-cache refresh; it does not modify grants or source data. All application catalog writes go to the disposable test database. Tokens/passwords were entered without terminal echo and kept in process environment/memory; no raw credentials are included in this document, code, or captured logs.

## Shared connector handling

The shared guard consumes existing unread-prefix/type hooks (including Qlik and SQL routines) and Power BI diagnostics. An audit of discovery methods and their client helpers also identified swallowed errors in SQL drivers, file sources, BI sources, search engines and monitoring catalogs. Exhausted discovery/fallback paths now mark the result incomplete; successful fallback paths remain usable. Optional descriptive enrichment can still fail when the authoritative listing/schema remains complete. Explicit cancellation is preserved rather than swallowed by those handlers.

A source that explicitly attests a complete empty authorized listing can still establish real removal. Legacy clients must use `DiscoveryResult([], complete=True)` to clear an existing catalog; a bare `[]` is retained conservatively as an unconfirmed empty result. A source that cannot finish the listing or introspection cannot authorize removal. This deliberately means that a permission-denied/unreadable model blocks publication until access or configured discovery scope is corrected.

## Boundaries

Atomicity covers application catalog rows, linked agent-table selections, per-user overlays, and successful-index metadata **per connection**, not all connections in one global transaction. Discovery progress and failure diagnostics persist separately. External OAuth refreshes, file cache artifacts and optional post-publication cache warming are outside the catalog transaction. No source database, permissions, or upstream data is changed.

This prevents future selection loss. It cannot reconstruct activation flags already deleted in the reported incident; those require a trustworthy historical snapshot or customer reselection.

## Review follow-up — verified regressions and resolutions

Review exposed five further failing cases, reproduced before the follow-up fix: MySQL and ClickHouse swallowing exhausted discovery errors, an error translation placed in the voice-input namespace, a legacy empty result clearing an existing catalog, and positive auto-selection being skipped under `commit=False`. The reproduction run showed **5 failed**. The focused suite after the fixes passed **25 cases**, subsequently expanded with optional-content and validation-idempotence coverage.

- Added failure signals to MySQL and ClickHouse and audited required discovery helpers with Python 3.13. The follow-up also marks omitted PBIX model schemas, incomplete SAP Datasphere columns, and missing Aria dynamic metric tables incomplete.
- Added a defensive empty-result check to shared catalogs, tool catalogs and personal overlays. Clearing a populated catalog now requires explicit complete-result attestation, protecting legacy connectors whose error handling has not adopted the failure contract.
- Moved the translated error into the actual top-level `errors` object in all ten catalogs. The new regression resolves the returned error code against each catalog; structural drift alone had not detected the shared namespace error.
- Optional file content-keyword extraction no longer blocks network-directory, S3 or on-premises SharePoint metadata listing. A failed CSV column describe or Salesforce object describe remains incomplete required schema discovery; silently publishing it could erase existing column or object metadata.
- Adapter validation now returns a list-compatible validated snapshot. Services reuse it without re-running diagnostics hooks, while still validating custom async client overrides.
- Positive requested auto-selection now runs inside the caller's transaction and flushes when `commit=False`; a regression verifies selection and rollback. Background reindex continues preserving opt-in selections. The old onboarding limit was already zero, so default new-table activation was not regressed by the switch to `None`.
- Kept the lowercase error-code wire value: the enum already contains lowercase values such as `saml_login_failed`, `validation`, and `access.denied`. Locale lookup is tested against the actual wire value.

The first broader follow-up run caught a completeness certificate being discarded when tools were normalized from an empty validated list back to a bare list. Removing that normalization retained the certificate and restored the explicit-empty tool test. No production credentials, hostnames, source identifiers, or personal information are included in these regressions or review notes.

Final review verification: **78 passed on SQLite**, **78 passed on PostgreSQL**, and **361 affected-client cases passed**. No outstanding failures in those runs.
