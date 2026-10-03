# Credential editing preserves saved values

Change must unlock existing identifiers without fetching secrets. Empty secret
inputs preserve the saved value; Cancel restores the saved identifiers. Tests
and saves send only changed non-empty credentials.

## Root causes

- `frontend/components/datasources/ConnectForm.vue` rendered literal dots for
  every locked credential and Cancel cleared every field. Generic edit callers
  discarded `credentials_meta`, so even allowed identifiers were never hydrated.
- `backend/app/routes/connection.py` exposed only OAuth-oriented identifiers;
  tenant ID and database username were absent from its admin-only allowlist.
- `backend/app/services/connection_service.py` preserved only four secret keys
  when replacing credentials. Partial updates could erase other identifiers or
  passwords. The existing test-override path already merged non-empty values.

## Reproduce and verify

From backend:

```sh
TESTING=true .venv/bin/python -m pytest tests/e2e/test_connection_credential_edit.py --db=sqlite -q --tb=short --disable-warnings
TESTING=true .venv/bin/python -m pytest tests/e2e/rbac/test_rbac_connections.py -k credential_metadata --db=sqlite -q --tb=short --disable-warnings
```

The first test uses real API operations and a local SQLite source, then reads
back the encrypted credential record. Before the backend fix all three cases
failed: rotating a secret, changing a username, and leaving a password blank
lost saved values. After the fix all three passed. No real credentials used.
The authorization test checks admin metadata vs the redacted response for a
member with access to a linked agent.

With the frontend running on port 3100, from frontend:

```sh
node tests/data_sources/credential-edit-flow.mjs
node tests/data_sources/connection-edit-flow.mjs
```

Both passed. The Power BI test uses actual field definitions and synthetic API
responses: tenant/client IDs survive Change and Cancel; secret rotation sends
only the new secret to both test and update; failed testing prevents saving.
The PostgreSQL flow checks username preservation. Both cover Hebrew and mobile.
The UI marks changed settings as requiring a new test, retaining test history.

Evidence lives in `media/pr/credential-edit/` and `media/pr/connection-edit/`.
The before reference is the user's screenshot; after evidence uses synthetic
identifiers and secret values. Browser tests do not contact Power BI/PostgreSQL.

## Scope

The metadata allowlist remains admin-only and explicitly excludes passwords,
secrets, private keys and tokens. Untouched/blank credentials now share the same
merge semantics for testing and saving. This does not add a secret-removal flow
or change connector discovery reporting.

Final permission check passed: an org admin receives the allowed identifiers;
a non-admin with explicit connection-management and linked-agent access gets
`credentials_meta: null` and `has_credentials: false`. Test setup explicitly
provides both grants required by the existing detail route.
