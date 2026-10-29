# Feedback Loop — Infor EPM service-account authentication

Validate service-account OAuth through the saved-connection client construction,
connection test, cube discovery, and MDX execution. All test credentials and
responses are synthetic; only the external HTTP boundary and clock are mocked.

## Root cause

`backend/app/data_sources/clients/infor_epm_client.py` previously supported only
client-credentials token exchange and pre-issued bearer tokens. Its constructor
and the registry credential schemas could not represent service-account keys.

## Deterministic reproduction

From `backend/`:

```sh
TESTING=true .venv/bin/python -m pytest tests/unit/test_infor_epm_client.py -k service_account -q
```

Before implementation, the initial four cases failed: registry construction
raised `KeyError: 'ion_service_account'`, and direct construction rejected the
new credential arguments. These failures established the missing public contract.

## Change

- Register `ion_service_account` with required, masked access key/secret fields.
- Use password grant with HTTP Basic client authentication and form encoding.
- Reauthenticate on expiry or once on HTTP 401, including async result polling.
- Retain client-credentials and static bearer behavior and the existing default.
- Close and discard a session when initial authentication fails.

The credentials use the existing encrypted connection storage. No new database
columns, external dependencies, or discovery/query interfaces are required.

## Verification

```sh
TESTING=true .venv/bin/python -m pytest tests/unit/test_infor_epm_client.py -q --tb=short --disable-warnings
TESTING=true .venv/bin/python -m pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q --tb=short --disable-warnings
```

Observed: **33 connector tests passed; 12 connection/data-source API tests passed**.
The service-account tests construct a client through `ConnectionService` using
encrypted credentials, exercise sync and async discovery/query paths, validate
wire-level form encoding and Basic authentication, verify token reuse/expiry/401
renewal, and reject incomplete credentials and denied token exchanges.

These checks validate our client contract against synthetic HTTP responses.
They do not establish compatibility with a live Infor deployment.

## Local form check

Ran the installed frontend and backend against a separate temporary SQLite
sandbox, seeded through `tools/agent/seed_org.py`. Opened the Infor EPM connection
form, selected **ION Service Account**, and verified that both new fields render
as password inputs. Existing client-credentials and bearer options remain visible.

Evidence (empty credential fields, synthetic workspace):

- `media/pr/infor-epm-service-account/client-credentials.png`
- `media/pr/infor-epm-service-account/service-account.png`

The standard boot script could not download its build dependencies in this
sandbox; the installed Python environment and frontend build were used instead.
No live Infor credentials were available. Full live connection/discovery/query
validation remains a deployment check; the automated tests cover those paths at
the HTTP boundary.
