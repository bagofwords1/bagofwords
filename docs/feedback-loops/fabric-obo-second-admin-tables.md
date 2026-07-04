# Sandbox Feedback Loop — Fabric / Entra OBO "second admin sees no tables"

Reproduces and validates the reported bug: on a Microsoft **Fabric / PowerBI**
connection in an **Entra ID / OBO** (`auth_policy=user_required`,
`allowed_user_auth_modes=["oauth"]`) environment, the **creating admin sees all
tables**, but a **second admin** — who *also* has a valid delegated token and can
run queries — **sees no tables in the Tables Selector, and "Reload tables" does
not fix it.**

This doc is the runnable feedback loop used to confirm the root cause in a fresh
cloud sandbox.

---

## Root cause (validated)

Two independent app behaviors combine:

1. **Display scoping.** `DataSourceService.get_data_source_schema_paginated`
   (`backend/app/services/data_source_service.py:2058-2083`) — the endpoint the
   "full schema" Tables Selector uses — scopes a `user_required` source to the
   caller's **per-user overlay** (`UserDataSourceTable`) when
   `effective_auth == "user"`. With an empty overlay, the canonical-table query
   is scoped to `id IN ([])` → **zero rows**. Unlike the legacy
   `get_data_source_schema` (`:1979`), the paginated path has **no live-fetch
   fallback** to populate the overlay on a miss.

2. **Reload gap.** `refresh_data_source_schema` (`:3057-3129`) for a
   **shared-catalog** source (Fabric/PowerBI) refreshes only the **canonical**
   catalog (`ConnectionTable` → `DataSourceTable` via
   `sync_domain_tables_from_connection`) and **never writes the caller's
   `UserDataSourceTable` overlay**. So clicking "Reload tables" cannot populate
   the second admin's overlay.

The creating admin's overlay was populated out-of-band (OBO auto-provision /
first live fetch at connect time — `connection_oauth_service.auto_provision_connection_credentials`),
which is why they see tables and the second admin doesn't.

> Note: This is *not* the "Connect required / 403" path. The second admin has a
> valid token (`effective_auth == "user"`), confirmed live below.

---

## Environment setup (fresh sandbox)

The app targets **Python 3.12** (uses 3.12 f-string syntax). The sandbox default
`python` may be 3.11 — use 3.12 explicitly.

```bash
cd backend
pip install uv
uv sync --frozen --extra dev

# Required by bow-config.dev.yaml (database.url: ${BOW_DATABASE_URL})
export BOW_DATABASE_URL="sqlite:///db/app.db"
mkdir -p db
```

Tests run on SQLite by default; the autouse `run_migrations` fixture builds the
schema per test (`tests/conftest.py`).

---

## Loop A — App-logic reproduction (no live Azure needed)

Self-contained: seeds an `ms_fabric` data source with two token-holding admins
(admin1 with an overlay, admin2 without), then asserts the two claims. The
reload's connection-level schema fetch is stubbed (the canonical catalog is
already seeded), so no live Fabric/ODBC is required.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
/tmp/venv312/bin/python -m pytest \
  tests/e2e/test_fabric_second_admin_overlay_repro.py -v -s
```

**Observed (PASS):**

```
[display] admin1 sees total=2 rows=2; admin2 sees total=0 rows=0   # Claim 1
[reload]  admin2 overlay rows after reload=0; admin2 tables after reload=0  # Claim 2
[control] admin2 WITH overlay sees total=2 rows=2                  # scoping is the cause
2 passed
```

Iterate here: edit the candidate fix (e.g. populate-on-miss in the paginated
path, or have the shared-catalog reload also refresh the caller's overlay) and
re-run — `test_second_admin_...` should then show admin2 `total=2`.

---

## Loop B — Live OBO confirmation (real Entra tenant)

Confirms the premise that **both** admins can obtain delegated tokens (so both
are `effective_auth == "user"` / can run queries). Requires outbound network to
`login.microsoftonline.com` and **ROPC enabled** on the app registration.

Secrets are passed via **env vars only — never commit them**:

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
export BOW_ENTRA_TENANT_ID=...        # app registration tenant
export BOW_ENTRA_CLIENT_ID=...
export BOW_ENTRA_CLIENT_SECRET=...
export BOW_OAUTH_TEST_DEMO1_EMAIL=...
export BOW_OAUTH_TEST_DEMO1_PASSWORD=...
export BOW_OAUTH_TEST_DEMO2_EMAIL=...
export BOW_OAUTH_TEST_DEMO2_PASSWORD=...
export BOW_FABRIC_SERVER=...          # <id>.datawarehouse.fabric.microsoft.com

/tmp/venv312/bin/python -m pytest tests/integrations/test_oauth_delegated.py \
  -k "TestClientCredentials or TestROPCLogin or TestOBOExchange or TestOBOServiceFunction" -v
```

**Observed (PASS, 10/10):** client_credentials (SP) for Graph/Fabric/PowerBI;
demo1 & demo2 ROPC login; OBO exchange demo1 & demo2 → Fabric token; OBO PowerBI
demo1; `exchange_obo_token` for Fabric/PowerBI.

> The `TestFabricClientDelegated` SQL tests (demo1 sees all / demo2 sees only
> sales) additionally need `pyodbc` + **ODBC Driver 18 for SQL Server**, which is
> not installed in this sandbox — they `skip` here. They exercise upstream Fabric
> ACLs, not the app overlay bug, so they're not required for this validation.

---

## What this proves

- Live: a second admin **can** get a valid delegated token and run queries
  (premise confirmed) — so the "no tables" symptom is **not** missing auth.
- App: with a token but **no per-user overlay**, the paginated Tables Selector
  shows **zero** tables, and the shared-catalog **reload never populates that
  overlay** — so reload can't fix it. Bug confirmed and isolated to the overlay
  scoping + reload paths above.

## The fix

`DataSourceService._refresh_shared_user_overlay` (`data_source_service.py`), wired
into `refresh_data_source_schema`'s shared-catalog branch: after the canonical
refresh, if the source is `user_required` and the caller resolves to
`effective_auth == "user"`, refresh the **caller's** per-user overlay
(`get_user_data_source_schema`) and return their user-scoped tables. Admins on the
service account (`effective_auth == "system"`) still get the full canonical
catalog; disconnected callers (`"none"`) get nothing (no canonical leak).

Result (Loop A, after fix):

```
[before] admin1 sees total=2; admin2 sees total=0   # overlay-scoped, empty
[after]  admin2 overlay rows=2; admin2 tables=2      # reload populated it now
```

> Regression note: some `tests/e2e/test_connection_oauth_flow.py` tests fail in
> this sandbox with `create_user -> 404 Not Found` (the HTTP signup route isn't
> available under this config). This is **pre-existing and unrelated** — it
> reproduces with the fix stashed. The repro/fix test seeds users directly to
> avoid that harness dependency.
