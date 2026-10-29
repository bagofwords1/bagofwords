# Feedback loop — administrators cannot revoke custom OAuth sessions

Members exposed removal but no session-revocation action. The existing admin
sign-out endpoint incremented the user's session epoch; custom OAuth access
and refresh tokens and unused authorization codes remained usable.

## Reproduce and verify

From `backend/`:

```sh
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db .venv/bin/pytest \
  tests/e2e/test_session_revocation.py tests/e2e/test_oauth_app.py \
  tests/e2e/test_oauth_mcp.py --db=sqlite -q
```

The three new parameterized cases in `test_admin_revocation_invalidates_oauth_credentials`
failed on the base implementation: access remained accepted and both refresh
and authorization-code redemption returned HTTP 200. With the fix: **36 passed**.
The same suite against a disposable PostgreSQL database using `--db=external`
and `TEST_DATABASE_URL`: **36 passed**. Entra token-exchange regressions:
**20 passed, 1 skipped** on SQLite.

Fixtures create an administrator, invite/register a member, and authorize a
mixed app/MCP client through the real HTTP endpoints. Revocation must reject
the member's JWT and OAuth credentials, preserve another user's tokens, and
allow a fresh login and authorization. Existing cases cover a non-admin's
rejected revocation attempt.

## Mechanism

- `backend/app/core/session_revocation.py`: atomically increment the epoch and
  soft-delete the user's OAuth tokens and pending codes, across organizations.
- `backend/app/services/oauth_server_service.py`: serialize code issuance,
  redemption and refresh with revocation; re-read credentials after acquiring
  the lock so a waiting refresh cannot reuse a revoked row.
- `backend/app/services/entra_token_exchange.py`: check the epoch again after
  provider IO and lock during local issuance.
- `frontend/components/MembersComponent.vue`: permission-gated action,
  confirmation, pending state and success/error feedback. All ten catalogs
  include the new strings; existing catalog drift is unchanged.

PostgreSQL locks only the affected user row. SQLite serializes writers using
its normal write lock. No migration is needed. Ordinary logout/password-change
behavior is unchanged; this change extends the administrative endpoint.

## Manual checks

In a disposable local sandbox, manually signed in with a real Entra demo
account. The SSO client and the access-token audience were different app
registrations; login requested `offline_access`. Called the admin endpoint:
HTTP 204, epoch **1 → 2**. Reloading the user's browser produced a **401** for
`/api/users/whoami` and returned to sign-in. Microsoft reauthorization succeeded
and the database contained a different refresh token. Screenshots containing
the real demo account are kept private, outside the repository.

Five simultaneous refresh/revoke trials against the running SQLite sandbox
passed: when refresh returned 200 before revocation, its newly issued token was
also rejected with 401 after both requests completed. This is not an exhaustive
PostgreSQL concurrency test.

## Limits

This revokes BOW-issued sessions, not Microsoft sessions, API keys, in-flight
work, or stored downstream credentials. A new external Entra assertion exchange
can authenticate again. Customer OAuth applications must handle 401 / invalid_grant
by starting authorization. Reauthorization may reuse Microsoft's browser session.
Directory synchronization and Fabric queries were **not** reverified in this
manual test. A fresh refresh token still requires the configured `offline_access`
scope. Nothing was deployed to production.
