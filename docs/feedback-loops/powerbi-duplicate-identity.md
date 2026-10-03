# Feedback loop — schema refresh fails with a missing catalog row ID

A connection can contain a plain and a qualified display name for the same
Power BI workspace/model/table. Syncing that historical catalog into an agent
must preserve selections and user grants, update columns, and be repeatable.
All evidence below uses synthetic records and stubbed Power BI HTTP responses.

## Root cause (validated)

At baseline `07c8cb753`, `backend/app/utils/powerbi_catalog.py:40` reconciles a
qualified name to an existing plain name. Its name-keyed result overwrites an
entry when two rows have the same identity. The domain sync then looks up both
original IDs in `backend/app/services/data_source_service.py:6511`, raising
`KeyError` for the overwritten row.

The shared-catalog upsert had another identity hole: it reused a row under a
changed display name only for authoritative discovery. A delegated discovery
could therefore insert another row for an existing identity after a model rename.
Concurrent discoveries also read the catalog before network I/O and then wrote
from those stale snapshots. These are code paths capable of creating duplicates;
this loop does not claim to establish the historical origin of any deployment's
records.

## Deterministic reproduction

Use Python 3.12 and install the backend dev dependencies with `uv sync --frozen
--extra dev`. From `backend/`:

```sh
export BOW_DATABASE_URL=sqlite:///db/app.db
mkdir -p db
uv run pytest tests/e2e/test_powerbi_refresh_identity.py -k duplicate_identity -q
```

Before the fix, both selected-row variants failed with `KeyError` at the domain
name lookup. The regression seeds the historical duplicates directly because
supported writes must no longer be able to create them. It replaces only the
external HTTP transport; connection discovery, schema services and DB are real.

## Fix

- A versioned, model-independent transactional repair consolidates duplicate
  connection identities. It retains the oldest canonical connection-table ID,
  and favors an active domain-table ID within each agent. It relinks personal
  overlays, statistics, usage and feedback before deleting redundant rows.
  Personal column grants, accessibility flags and overlay names are unchanged.
- Migration `pbiidentity01` repairs historical rows, then adds a unique JSON
  expression index on connection/workspace/model/table for valid Power BI
  identities. Custom tables and incomplete identities are excluded.
- Both catalog upserts and direct agent sync serialize writes and run the
  repair. After slow external discovery, upserts re-read the catalog under the
  connection lock. PostgreSQL uses a row lock; SQLite uses a writer lock.
- Delegated discovery reuses an existing identity even if its label changes,
  without replacing shared columns or widening personal access.

The regression also runs the migration against populated historical records,
checks selection IDs and usage counts, preserves inaccessible/masked personal
columns, and verifies two successive live-schema refreshes using the fake API.
Separate tests force simultaneous discoveries and exercise the DB uniqueness
backstop across different workspaces, models and tables.

## Verification

```sh
uv run pytest tests/e2e/test_powerbi_refresh_identity.py \
  tests/e2e/test_powerbi_catalog_uniqueness.py \
  tests/unit/test_powerbi_schema_refresh.py \
  tests/unit/test_powerbi_refresh_user_permissions.py -q
```

SQLite: **43 passed**. Isolated local PostgreSQL 14: **43 passed**.
The Docker PostgreSQL 15 test container failed to start (120-second readiness
timeout), so the PostgreSQL leg used a disposable local cluster via `--db=external`.
Run the same command with `--db=postgres` for the
Testcontainers leg, or `--db=external` with `TEST_DATABASE_URL` pointing to an
isolated disposable PostgreSQL database. The external fixture resets its schema;
never point it at application data.

The pre-existing synthetic HTTP response now supplies response bytes as well as
JSON, matching the connector's `execute_dax_rows` transport contract.

## Scope and limits

This repairs canonical identity duplication. It does not invalidate every user's
schema cache after a shared refresh, alter refresh UI, or call a live provider.
The repair preserves per-user schema data rather than replacing it with the
service account's schema. Downgrading removes the uniqueness index but does not
recreate consolidated duplicate rows. No deployment is performed by this loop.
