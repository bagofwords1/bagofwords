# Backend-only recovery of missing Entra connection credentials

## Root cause

Recovery previously filtered connection OAuth client IDs against the SSO client
ID. Those can legitimately differ: the configured `api://<connection-app-id>/<scope>`
is the recipient of the stored access token. Correctly configured separate-app
connections were skipped before any OBO exchange.

The regression test produced zero credentials on base `8c26722a9`, where five
connections should share one exchange. The same-app case already passed.

## Final change

- Derive connection app targets from explicitly configured Entra API scopes.
- Try the stored access token without requiring a refresh token. If refresh is
  available, request only that resource's scopes, not mixed Graph/API scopes.
- Ordinary completion requests attempt recovery before execution, bounded to
  20 seconds. Existing background triggers still work. No custom header, new
  HTTP response, frontend change, or customer-app change is required.
- Keep per-user PostgreSQL advisory locking, shared exchange caching and catalog
  sync. No migration or database-wide lock is introduced.
- Filter fresh-login provisioning to active connections in current memberships.
- Background/prompt recovery respects explicit Disconnect. A fresh Entra login
  can reactivate that row using the existing sign-in behavior.

An expired or wrong-audience assertion with no refresh token cannot be repaired
by the backend. Access remains denied through existing checks until fresh Entra
login through the customer's existing flow. The earlier experimental browser
reauthorization protocol and example-app changes were removed from this patch.

## Reproduce and verify

From `backend/`, using Python 3.12 and the normal project dependencies:

```sh
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db python -m pytest \
  tests/e2e/test_oauth_connection_recovery.py \
  tests/unit/test_obo_recovery_per_user.py \
  tests/unit/test_obo_stored_login_recovery.py -q
```

**37 passed.** The E2E tests use an ordinary custom OAuth app token, no special
header, and admin/member roles. They seed usable, wrong-audience and expired
stored assertions without refresh tokens. A usable assertion creates credentials
before the existing missing-LLM validation response; the other cases create none.
They also verify explicit Disconnect and fresh-login reactivation. Only external
Entra HTTP and Fabric schema discovery are mocked.

For PostgreSQL, set `TEST_DATABASE_URL` to a disposable database and run:

```sh
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db python -m pytest \
  tests/e2e/test_oauth_connection_recovery.py \
  tests/unit/test_obo_recovery_per_user.py \
  tests/unit/test_obo_provisioning_concurrency.py \
  tests/e2e/test_oauth_app.py --db=external -q
```

**41 passed** on PostgreSQL, including ordinary OAuth apps and advisory locks.

## Live evidence and limits

Used an isolated local database and the supplied demo tenant/account, with
separate SSO and Power BI applications. The Entra account had no refresh token.
Real Microsoft authorization and a delegated Power BI `executeQueries` request
returned HTTP 200, and catalog sync discovered 63 user-visible tables.

The live Disconnect → BOW logout → Entra sign-in check reactivated the same row
and removed its disable marker. No customer or production database was changed.
Full AI report generation was not verified: the sandbox has no usable LLM key.

Backend-only stored-token recovery is verified separately by removing only the
disposable demo credential and invoking the production recovery service with
its stored Entra assertion, without another browser authorization. Secrets and
raw tokens remain outside this repository.

The backend-only live attempt recreated one active credential with no refresh
token. Its delegated Power BI query returned HTTP 200 and the expected value 1.
