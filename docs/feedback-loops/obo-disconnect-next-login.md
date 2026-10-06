# OBO Disconnect lasts until the next successful SSO provisioning

Disconnect previously left an inactive `auto_recovery_disabled` credential row
that both login provisioning and stored-login recovery skipped forever.
The requested behavior is to keep background recovery blocked while allowing
a fresh Entra login to reconnect after a successful delegated token exchange.

## Reproduction and verification

Run from `backend`:

```sh
TESTING=true BOW_DATABASE_URL=sqlite:///db/test.db python -m pytest \
  tests/unit/test_obo_recovery_per_user.py \
  tests/unit/test_obo_stored_login_recovery.py \
  tests/unit/test_obo_provisioning_concurrency.py \
  tests/unit/test_obo_background_database.py --db=sqlite -q
```

The changed login regression failed before the service change: expected one
provisioned connection after Disconnect and login, but received zero.
Microsoft HTTP is stubbed; provisioning, Disconnect, and database writes are real.
The regression checks that background recovery leaves the user disconnected,
a rejected exchange preserves the opt-out, and a successful login exchange
reactivates the same row without duplicates and removes the opt-out.

`connection_oauth_service.py` now limits the opt-out checks to `missing_only`
recovery. Login reuses the inactive marker after exchanging the assertion and
rechecking eligibility and concurrent credential changes. No database migration.
Concurrency tests exercise both login and recovery for admin and member users,
including Disconnect, manual Connect, membership removal, and connection disable
while the Microsoft token request is pending.

SQLite focused suite: 45 passed. Repeat the per-user and concurrency files with
`--db=postgres` to exercise the real PostgreSQL advisory locks.

This is a local code verification, not a production deployment or a new live
Entra verification. Existing login/recovery job-admission behavior is unchanged.

PostgreSQL verification: **25 passed** (per-user and concurrency files, including
both provisioning paths). Docker disk was full, so the standard testcontainer
could not initialize. Verification used an isolated `postgres:16` container with
RAM-backed PGDATA and `--db=external`; no existing Docker data was deleted.
Changed test files pass Ruff. The service has the same 28 existing Ruff findings
as HEAD, checked with the same repository configuration. `git diff --check` passes.

Current-main PR branch verification: **53 focused SQLite tests passed**.
