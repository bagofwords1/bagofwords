# Feedback Loop — Power BI / Fabric OBO: creating admin sees ZERO tables until they sign in

Reproduces the reported flow: on an **Entra ID / OBO** connection
(`auth_policy=user_required`, `allowed_user_auth_modes=["oauth"]`) for
**Power BI / Microsoft Fabric**, the admin creates the connection, then creates
a new agent and selects that connection — and the **Select Tables step shows
zero tables**. Clicking **Reload tables** does nothing (the failure is silently
swallowed). The admin saves with an empty selection, then **signs in** to the
agent's connection (Microsoft OAuth) — and only then do the tables appear.

Validated live in a fresh cloud sandbox against a real Entra tenant on
2026-07-15. Sibling loop: `fabric-obo-second-admin-tables.md` (same overlay
machinery, different actor).

---

## Root cause (validated)

The schema UI for delegated (OBO) connections is **identity-scoped and
fail-closed**, and the creating admin has no delegated token yet:

1. **Display scoping (the zero-tables symptom).**
   `DataSourceService.get_data_source_schema_paginated`
   (`backend/app/services/data_source_service.py` — "Identity-aware scoping"
   block) classifies the caller via `_resolve_effective_auth` →
   `build_token_identity_status` (`backend/app/services/connection_identity.py`).
   For a token-supporting connection the default query identity is **"self"**,
   and with no `UserConnectionCredentials` row the status is
   `effective_auth="none"` → `overlay_table_ids = []` → the canonical-table
   query is scoped to `id IN ([])` → **zero rows**, even though the canonical
   catalog (`ConnectionTable`) is fully indexed. The service principal is never
   used silently for an admin's interactive views — by design ("no silent SP
   fallback").

2. **Reload fails, silently.** The wizard's Reload calls
   `GET /data_sources/{id}/refresh_schema` → `ConnectionService.refresh_schema`
   → `construct_client` → `resolve_credentials`
   (`backend/app/services/connection_service.py`), which for a user with no
   token and identity "self" raises
   **403 "Connect required: this connection runs queries with your own
   credentials…"**, re-wrapped by `refresh_schema`'s catch-all as
   **500 "Failed to refresh schema: 403: Connect required…"**. The frontend
   swallows it: `TablesSelector.onRefresh` has
   `catch (e) { /* Swallow refresh errors */ }`
   (`frontend/components/datasources/TablesSelector.vue`). Observed live:

   ```
   /refresh_schema HTTP 500: {"detail":"Failed to refresh schema: 403: Connect
   required: this connection runs queries with your own credentials. Connect
   your account or switch to the service account."}
   ```

3. **Sign-in fixes it** because the OAuth callback
   (`backend/app/routes/connection_oauth.py`) stores the delegated token and
   runs the overlay sync (`get_user_data_source_schema`), after which
   `effective_auth="user"` and the same paginated endpoint returns the user's
   overlay tables.

4. **The wizard has no auth awareness.** The schema step
   (`frontend/pages/agents/new/[id]/schema.vue` → `AgentKnowledgeTabs` →
   `TablesSelector`) renders no "Connect your account" prompt and no hint of
   why the list is empty; the Connect affordance only exists on the agent page
   after save — which forces the observed create → save → sign in → tables
   sequence.

The same pattern applies to every `user_required` + `oauth` connector
(`powerbi`, `ms_fabric`, `sharepoint`, `onedrive`, `outlook_mail`, ServiceNow
OAuth) and to any member opening the tables view before connecting.

---

## The loop (live, real Entra tenant)

Secrets come from **env vars only — never commit them**:

```bash
export BOW_ENTRA_TENANT_ID=...            # tenant
export BOW_ENTRA_CLIENT_ID=...            # OAuth app for user sign-in / OBO
export BOW_ENTRA_CLIENT_SECRET=...
export BOW_PBI_MASTER_CLIENT_ID=...       # service principal for the catalog
export BOW_PBI_MASTER_CLIENT_SECRET=...
export BOW_OAUTH_TEST_DEMO1_EMAIL=...     # Entra user that signs in
export BOW_OAUTH_TEST_DEMO1_PASSWORD=...
```

Environment (fresh sandbox):

```bash
cd backend && pip install uv && uv sync --frozen --extra dev
# OBO on tabular sources is enterprise-gated — mint a sandbox license:
LIC=$(uv run python scripts/gen_sandbox_license.py 10)   # backs up the pem; restore before committing
export BOW_LICENSE_KEY="$LIC"
export BOW_CONFIG_PATH=$PWD/../configs/bow-config.dev.yaml   # base_url http://localhost:3000
export BOW_ENCRYPTION_KEY=$(uv run python -c "from app.settings.bow_config import generate_fernet_key; print(generate_fernet_key())")
cd .. && tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
```

Run the UI repro (screenshots land in `/tmp/bow-agent/obo-repro-media`):

```bash
node tools/agent/e2e_obo_zero_tables_repro.mjs
```

**Observed (2026-07-15):**

```
3c. save connection            -> modal: "Discovered 6 tables in 4s"   # SP-seeded catalog
5.  indexing status: completed
    CANONICAL CATALOG TABLES: 6 -> deals2/…, leads/…, mySM/…, SalesPush/Sales, SalesPush/Customers
6.  wizard Select Tables       -> "Showing 1-0 of 0 · No tables found"  # THE BUG
6b. click Reload               -> /refresh_schema HTTP 500 ("403: Connect required…"), UI silent
8.  agent page: Connect chip + "Sign in" badge; click Connect ->
    redirect to https://login.microsoftonline.com/<tenant>/oauth2/v2.0/authorize
    (client_id = OAuth app, redirect_uri = <base_url>/api/connections/oauth/callback,
     scope = https://analysis.windows.net/powerbi/api/.default offline_access)
```

**Sandbox caveat:** the cloud sandbox's egress proxy rejects Chromium's TLS
handshake, so the hosted Microsoft login page can't render in the headless
browser (curl/python through the same proxy reach the same hosts fine). On a
normal network the script completes the login form itself. In the sandbox,
finish the sign-in with the equivalent delegated-token path — the ROPC grant
(same pattern as `tests/integrations/test_oauth_delegated.py`), followed by the
exact callback code path (upsert `UserConnectionCredentials` → verify →
overlay sync):

```bash
cd backend && uv run python ../tools/agent/e2e_obo_signin_ropc.py
# user=… connection=… auth_policy=user_required
# delegated token acquired for demo1@… (scope: powerbi)
# UserConnectionCredentials upserted
# test_user_connection: success=True message=Connected to Power BI. Verified query access on dataset 'deals2' in workspace 'BOW'.
# overlay sync for data source …: 6 tables
DS_ID=<data source id> node tools/agent/e2e_obo_after_signin.mjs
# full_schema for admin now: total_tables= 6 rows= 6
```

The **same** wizard step and the **same** `full_schema` endpoint that returned
0 rows before sign-in now return **6 tables**.

---

## Screenshots (assets/)

| step | file |
|---|---|
| Connection form: SP creds + "Require user authentication" + OAuth app | `obo-zero-tables-01-connection-form.png` |
| Test Connection passes (SP reaches the tenant) | `obo-zero-tables-02-test-passed.png` |
| Save: indexing discovers **6 tables** | `obo-zero-tables-03-discovered-6-tables.png` |
| Wizard Select Tables right after: **"No tables found", 1-0 of 0** | `obo-zero-tables-04-wizard-zero-tables.png` |
| After Reload: still zero (500/403 swallowed) | `obo-zero-tables-05-reload-still-zero.png` |
| Agent page pre-sign-in: 0 tables, Connect chip, Sign in badge | `obo-zero-tables-06-agent-before-signin.png` |
| Connect click → Microsoft authorize URL (unreachable in sandbox) | `obo-zero-tables-07-redirect-to-microsoft.png` |
| Same wizard step after sign-in: **6 tables** | `obo-zero-tables-08-wizard-after-signin.png` |
| Agent page: 6/6 tables active | `obo-zero-tables-09-agent-tables-visible.png` |

---

## The fix (validated live, 2026-07-15)

Four changes, all display/UX-level — query execution keeps the fail-closed
"no silent SP fallback" behavior:

1. **Owner/admin sees the canonical catalog before first sign-in.**
   `DataSourceService._admin_catalog_access` (data source owner, org admin, or
   `manage_connections`) gates a display fallback in
   `get_data_source_schema_paginated`, `get_data_source_schema`, and
   `_refresh_shared_user_overlay`: `effective_auth == "none"` no longer empties
   the list for that audience (they already see the same names via
   `GET /connections/{id}/tables`). Plain members without a token still get
   zero rows.
2. **Reload works pre-sign-in and errors are surfaced.**
   `ConnectionService.refresh_schema` indexes with the connection's system
   creds (`current_user=None`, the background indexer's identity) when the
   caller has no per-user token, instead of 403ing; deliberate `HTTPException`s
   are no longer re-wrapped as 500. The frontend (`TablesSelector.onRefresh`)
   now toasts failures instead of `catch {}`.
3. **The wizard explains itself.** `TablesSelector` fetches the agent's
   connections (now served with `auth_policy` / `allowed_user_auth_modes` /
   `user_status`) and renders: a **"Connect your account"** empty state when a
   delegated connection has no rows to show, and an admin banner ("You're
   viewing the full catalog as an admin… connect your account") when the
   canonical fallback is in effect.
4. **Sign-in returns to where it started.** `/oauth/authorize?return_to=<path>`
   rides in the signed OAuth state (`rt` claim, app-internal paths only) and the
   callback redirects there — e.g. back to `/agents/new/{id}/schema`.

**Observed after the fix (same loop, fresh connection):**

```
5.  indexing completed, canonical catalog = 6 tables
6.  wizard Select Tables       -> Showing 1-6 of 6 + admin banner   # was 0
6b. click Reload               -> /refresh_schema HTTP 200, 6 tables # was 500/403 swallowed
    select all -> Save         -> agent page shows 6/6 active — all BEFORE sign-in
    banner Connect             -> GET /connections/{id}/oauth/authorize?return_to=/agents/new/{id}/schema
    delegated-only conn (no SP catalog) -> "Connect your account" empty state
    reload on a broken SP      -> red toast with the real Azure AD error     # was silent
    after sign-in              -> same 6 tables, banner gone (overlay view)
```

Regression tests (`backend/tests/e2e/test_obo_admin_catalog_before_signin.py`):

```
[display] token-less owner sees total=2 rows=2; token-less member sees total=0 rows=0
[reload]  token-less owner reload returned 2 tables (system identity, no 403)
2 passed
```

The sibling loop (`test_fabric_second_admin_overlay_repro.py`) still passes.
Pre-existing, unrelated failures: `tests/unit/test_connection_oauth.py`
`test_ms_fabric` / `test_obo_exchange_ms_fabric` assert the old
`api.fabric.microsoft.com` scope (reproduce with the fix stashed).

After-fix screenshots:

| state | file |
|---|---|
| Wizard right after creation: **6 tables + admin banner** (no sign-in) | `obo-fixed-01-wizard-tables-pre-signin.png` |
| Reload now succeeds (HTTP 200, list intact) | `obo-fixed-02-reload-succeeds.png` |
| Select all pre-sign-in | `obo-fixed-03-select-all-pre-signin.png` |
| Agent page: 6 tables active, still not signed in | `obo-fixed-04-agent-page-pre-signin.png` |
| Delegated-only (empty catalog): "Connect your account" empty state | `obo-fixed-05-empty-catalog-connect-prompt.png` |
| After sign-in: same 6 tables, banner gone (overlay view) | `obo-fixed-06-wizard-after-signin.png` |
| Reload failure now surfaces a toast with the provider error | `obo-fixed-07-reload-error-toast.png` |

## What this proves

- The canonical catalog **is** populated at connection creation (SP-seeded
  background indexing — "Discovered 6 tables"), so the empty selector is not a
  discovery failure.
- The zero-tables wizard, the silent Reload failure, and the tables appearing
  only after per-user sign-in are all consequences of the fail-closed
  identity scoping (`effective_auth="none"` for a not-yet-connected caller)
  plus the wizard's lack of any connect prompt and the swallowed refresh error.
- A fix is UX-level: surface auth state in the wizard (prompt Connect / offer
  the service-account identity to admins), stop swallowing the reload error,
  or show the canonical catalog read-only until the caller connects — not a
  change to the credential model.

## Regression notes

- `test_connection_params` (Test button) validates with the SP credentials even
  for `user_required` — it passing while the selector shows nothing is part of
  what makes the UX confusing.
- The modal's indexing panel ("Discovered 6 tables") and the very next wizard
  step ("No tables found") disagree — same session, seconds apart.
