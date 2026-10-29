# Feedback loop — external Entra sign-in through custom OAuth apps

An application that already authenticates with Entra should call BOW and use
per-user Power BI without another BOW sign-in. Enable this explicitly on its BOW
OAuth registration. First-time users must have a current invitation in that
registration's organization or qualify for its enabled domain signup policy.

## Root cause and contract

The base revision `1db1cf747` only accepts authorization-code and refresh-token
grants in `backend/app/routes/oauth_server.py`. An Entra access token cannot be
used as a BOW bearer token. The initial regression returned
`unsupported_grant_type` when expecting a successful token exchange.

The new route uses `app/services/entra_token_exchange.py` to verify Microsoft's
signature, issuer, tenant, audience, lifetime, delegated scope and external
client ID. `app/services/entra_admission.py` resolves immutable identity or admits
a new user through the existing invitation/domain rules. It never claims an
existing account just because its email matches. The shared OBO provisioning
service stays responsible for connection credentials and catalog synchronization.

## Loop A — deterministic regression

Python 3.12, installed backend dev dependencies:

```sh
cd backend
BOW_DATABASE_URL=sqlite:///db/test.db TESTING=true uv run pytest \
  tests/e2e/test_entra_token_exchange.py tests/e2e/test_oauth_app.py \
  tests/e2e/test_oauth_mcp.py tests/e2e/test_oauth_mcp_multiorg.py \
  --db=sqlite -q
```

The Microsoft discovery, signing key and token endpoints are boundary fixtures;
JWT signing/validation, routes, database, admission and credential writes are real.
No external credentials are required. Tests cover invalid/forged tokens, v1/v2,
opt-in, confidential-client authentication, scopes, membership, roles, invitation
expiry, domain policy, seat caps, idempotent admission, Disconnect, provisioning
before token issuance, and ordinary PKCE/refresh compatibility.

PostgreSQL (use a disposable database; the fixture resets its schema):

```sh
BOW_DATABASE_URL=sqlite:///db/test.db TESTING=true \
TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55439/exchange_test \
uv run pytest tests/e2e/test_entra_token_exchange.py --db=external -q
```

The PostgreSQL-only concurrent-admission test verifies one user and identity with
parallel first exchanges. Admission uses transaction advisory locks for the
subject and organization, plus an identity uniqueness index. It holds no lock
across Microsoft network calls. Contending requests receive retryable 503.

Observed during development: the original grant failed; after implementation the
SQLite OAuth compatibility suite passed 47 tests. PostgreSQL exposed an outer-join
`FOR UPDATE` error introduced by admission; limiting the row lock to Organization
fixed it (20 exchange/admission tests passed before adding concurrency coverage).
Final PostgreSQL count including concurrent admission: 21 passed. Existing OBO regression suite: 30 passed.
Frontend production build completed. The developer server must be stopped before
building; Nuxt deliberately rejects simultaneous dev/build in one checkout.

## Loop B — live Microsoft and Power BI

The authorized demo tenant was tested against an isolated local database, not
production. Real external Entra registration → BOW-audience access token → new
exchange endpoint → BOW API → persisted delegated Power BI credential → real DAX
`EVALUATE ROW("OBO_Probe", 1)` returned `1` for all three accounts:

- An existing Entra-linked user, initially denied without membership, then allowed
  after the local administrator added the user.
- Demo1, absent from BOW before the external-app sign-in, admitted through enabled
  domain signup, with no BOW registration/login screen.
- Demo2, absent from BOW before sign-in, admitted by invitation with domain signup
  disabled, also with no BOW registration/login screen.

The Power BI probe used `PowerBIClient.execute_query` with the credential written
by this new endpoint. It proves downstream delegation independently of an LLM.
An LLM/SSE completion and Fabric SQL were not part of this live verification.

Run the reusable local demo with environment variables (never commit secrets):

```sh
export BOW_URL=http://localhost:3017
export ENTRA_TENANT_ID=<tenant-guid>
export EXTERNAL_ENTRA_CLIENT_ID=<external-app-guid>
export BOW_ENTRA_CLIENT_ID=<bow-api-app-guid>
export BOW_CLIENT_ID=<bow-custom-app-client-id>
python tools/agent/entra_exchange_demo.py
```

The demo prompts invisibly for the two secrets unless `EXTERNAL_ENTRA_SECRET` and
`BOW_CLIENT_SECRET` are set. Register `http://localhost:3000/entra/callback` on the
external Entra registration, give it delegated `access_as_user` consent on BOW,
and opt the BOW custom app into this external client ID. Ensure invitations or
domain admission are configured before first sign-in. Tokens stay in memory.
This is a loopback-only development harness, not a production session store.

The local Nuxt proxy hard-codes a localhost:3000 forwarded host. For BOW SSO on
another local port, adjust that locally for the test and restore before commit.
The external token-exchange flow itself does not need a BOW browser callback.

## Scope, lifecycle and limitations

- Configuration defaults off; regular OAuth apps are unchanged.
- New accounts use the invitation/domain role, including pending invited RBAC.
  Email collisions with unlinked existing accounts fail closed.
- Only the app's organization and matching downstream tenant/client are considered.
- Exchange tokens last at most one hour, bounded by the Entra assertion lifetime.
  No BOW refresh token or raw incoming assertion is persisted.
- Disconnect is respected; this exchange does not undo a manual disconnect.
- The feature adds nullable configuration, identity and token-context fields plus
  a unique nullable identity key. Apply the migration before deploying the code.
- Public-cloud tenant GUID authorities and canonical `api://<client>/access_as_user`
  scopes are supported. Guest identity variants and sovereign clouds were not
  live-tested.
- Frontend screenshots and a captured three-frame form flow are under
  `media/pr/entra-exchange/`, including Hebrew RTL. The docs screenshot is staged
  under `docs/screenshots/pending-changes/external-entra-applications/`.
