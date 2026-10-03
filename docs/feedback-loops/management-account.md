# Feedback loop — Manage connection uses personal credentials

An admin with “My account” selected could not test or save a Power BI connection after signing out, even though valid organization credentials were saved. Management also displayed the personal status beside shared schema discovery.

## Root cause (validated)

`backend/app/routes/connection.py` passed the current user to the management test service. `ConnectionService.resolve_credentials` then followed the query-identity preference, rejecting a signed-out user before applying organization credential overrides. `frontend/components/EditConnectionModal.vue` also initialized its result from `user_status`.

## Deterministic reproduction

Run from `backend`:

```sh
TESTING=true .venv/bin/python -m pytest tests/e2e/rbac/test_connection_management_identity.py -q --tb=short
```

The initial two-case regression failed before the fix: the organization case returned `success: false`; the personal-only case had no management-scope metadata. The test now varies signed-in/signed-out users as well. It uses real routes, credential resolution, encrypted storage, and permission checks; only the external Power BI probe/discovery is stubbed. A synthetic OAuth callback credential row supplies the signed-in case.

## Fix

- `backend/app/services/connection_identity.py`: derive management authentication from the selected registry auth variant, per-user catalog ownership, and OAuth app/DCR registration modes. The presence of a credentials blob is not proof of organization access.
- The management test endpoint uses organization context when supported. User-only configurations retain personal context and cannot fall back to a service-account preference.
- Personal tests do not overwrite shared health. Management detail exposes explicitly scoped status and timestamps; the modal no longer borrows personal results for organization health.
- The modal labels **Organization account** or **Your account**, explains which credentials are used, and offers sign-in for unsigned personal OAuth configurations. Personal discovery polls the user scope; organization discovery retains the org scope.

## Verification

```sh
cd backend
TESTING=true .venv/bin/python -m pytest tests/e2e/rbac/test_connection_management_identity.py tests/e2e/test_connection_credential_edit.py tests/e2e/rbac/test_rbac_connections.py -q --tb=short --disable-warnings
```

Observed: **15 passed**. Covers valid/invalid organization credentials despite personal identity selection, personal-only signed-in/out behavior, no personal writes to shared health, credential retention, and member permission denial.

With the Nuxt development server on port 3100:

```sh
cd frontend
node tests/data_sources/manage-connection-flow.mjs
```

The actual Vue components are mounted on a temporary route with synthetic API fixtures. The flow checks account labels, independence from personal sign-out, personal sign-in redirection to a stub provider, disabled actions until sign-in, scoped discovery, edit/save/cancel, schedule persistence, and Hebrew/mobile layouts. This proves the UI contract, not a live OAuth provider exchange.

Evidence: `media/pr/management-account/{before,after,personal,he,mobile}.png`; flow recordings are produced under `media/pr/manage-connection/video/`.

No migration or query-preference update is performed. Previously cached results from before this fix need a fresh test to reflect the corrected account scope.
