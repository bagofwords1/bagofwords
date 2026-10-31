# Feedback loop — upgrading the released host application schema

`hostapp01` shipped in v0.0.601 (commit `95da1b81d`, published October 30,
2026). Customers who already applied it must reach the Connected applications
schema through a new revision, keeping their existing OAuth and embed identities.

## Root cause (validated)

Commit `1f2e9c322` rewrote
`backend/alembic/versions/hostapp01_host_applications.py` in place. Alembic
already records that revision in released installations, so `upgrade head`
never executed its new table creation. The current application then expected
`connected_applications` and capability tables that did not exist.

The original OAuth compatibility test downgraded and upgraded using the same
rewritten file. It checked row preservation inside the new implementation, but
never exercised the schema customers actually received.

## Deterministic reproduction

`backend/tests/fixtures/released_migrations/v0_0_601_hostapp01.py` is a frozen,
byte-for-byte copy from the release. The regression creates a scratch database
through release head `mrgheads11` using that file, seeds released-format records,
then runs the current Alembic chain normally. Direct legacy inserts are necessary:
the current API cannot create the old schema. The test also asserts the restored
production migration remains identical to the frozen release.

From `backend`, using the repository's Python 3.13 environment:

```sh
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db .venv/bin/python -m pytest \
  tests/unit/test_connected_application_upgrade.py --db=sqlite -q
```

Before the fix, this produced:

```text
AssertionError: A database already stamped with the released hostapp01 needs a NEW revision
1 failed
```

The fixture contains two OAuth registrations and three hosts with the same
name but distinct IDs, including disabled and soft-deleted records. It includes
all three host identity modes, keys, public IDs, epochs, creator IDs, Entra
configuration, artifact bindings, replay records, embed tokens, an OAuth access
and refresh token, and a pending PKCE authorization code.

## Fix

- Restore `hostapp01` exactly as released.
- Add `connapps01` after `payload01`. Copy registrations into the common parent
  and capability tables, then repoint the three child foreign keys **before**
  dropping legacy parents. This ordering preserves sessions and bindings when
  SQLite foreign-key enforcement is enabled.
- Preserve every legacy field and ID, including disabled/deleted state. Keep
  legacy agent ceilings unrestricted (`NULL`); user/artifact permissions still
  apply. New applications retain their existing default of no permitted agents.
- Refuse overlapping legacy IDs before any schema mutation. Never combine
  registrations by name.
- Support databases already holding the complete prerelease Connected
  applications schema without copying or rotating their identities again.
- Allow an immediate downgrade/re-upgrade of migrated legacy registrations.
  Refuse downgrades that would discard agent restrictions, OAuth disable state,
  dual-capability registrations, capability-free registrations, or host names
  too long for the old schema. Those states need a pre-upgrade backup for rollback.

The dashboard corpus deliberately stages later schema revisions without stamping
Alembic. Its harness now reverses only an unstamped application table move before
replaying released history; otherwise `hostapp01` would recreate existing child
tables. This adjustment belongs in the test harness, not in a shipped migration.

## Verification commands

Use a disposable database only; test fixtures reset their databases.

```sh
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db .venv/bin/python -m pytest \
  tests/unit/test_connected_application_upgrade.py \
  tests/e2e/test_connected_applications.py \
  tests/e2e/test_oauth_app.py tests/e2e/test_oauth_mcp.py \
  tests/e2e/test_entra_token_exchange.py \
  tests/e2e/dashboards/test_embed_host_applications.py \
  tests/e2e/dashboards/test_embed_identity_sources.py \
  --db=sqlite -q --disable-warnings
```

For PostgreSQL, run the same selection with `--db=postgres` and
`TEST_DATABASE_URL` set to a disposable PostgreSQL database. The observed run
used PostgreSQL 16 in a dedicated tmpfs container. No production database or
Microsoft credentials are required.

The released-schema test verifies field-for-field preservation, child foreign
keys, SQLite foreign-key checks, immediate rollback/re-upgrade, and refusal to
silently drop a new agent policy. The API regressions exercise existing access,
refresh and pending PKCE grants, plus admin/member embed sessions, existing
signing keys, artifact admission, replay rejection and subsequent revocation.

## Evidence and limits

Observed on October 31, 2026:

| Check | Result |
|---|---|
| Released-schema regression before fix, SQLite | 1 failed: missing Connected applications table |
| Final focused suite, SQLite | **12 passed** |
| Final focused suite, PostgreSQL 16 | **12 passed** |
| Broader OAuth/Entra/embed suite, PostgreSQL 16 | 107 passed, 1 failed before the admin fixture correction |
| Broader OAuth/Entra/embed suite, SQLite | 106 passed, 1 failed, 1 skipped before the admin fixture correction |
| Dashboard data migration / refresh-history corpus, SQLite | 33 passed, 1 failed before the unstamped fixture correction |
| New migration/test Ruff checks | Passed |
| Python parsing / whitespace / Alembic heads | 7 files parse; no whitespace errors; one head, `connapps01` |
| Released migration comparison | No diff against v0.0.601 |

The final 12-case selection is reproducible with:

```sh
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db .venv/bin/python -m pytest \
  tests/unit/test_connected_application_upgrade.py \
  tests/e2e/test_connected_applications.py \
  tests/e2e/dashboards/test_embed_host_applications.py::test_schema_upgrade_preserves_embed_sessions_keys_and_replay_protection \
  tests/e2e/dashboards/test_schema_refhist01.py \
  --db=sqlite -q --disable-warnings
```

Use the PostgreSQL environment and `--db=postgres` for the other leg. The initial broad failure was the new admin test
using an unset creator membership ID; it now resolves the membership through
the API. The corpus failure was `table artifact_host_applications already
exists` during replay of the released migration; the corrected round-trip
passes on both databases. These were introduced test-harness problems, not
unexplained failures attributed away from the change. The skipped SQLite case
requires PostgreSQL concurrency. Counts describe separate runs, not a summed
full-suite result.

This loop checks the database transition and real local API behavior. Entra tests stub
Microsoft's boundary; it does not repeat live Microsoft/OBO, browser UI,
ServiceNow, air-gap or full-repository verification. Earlier customer-style
browser evidence remains in `connected-applications.md` and
`erp-embed-entra-customer.md`.
