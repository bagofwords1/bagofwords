# Feedback Loop — dashboard upgrade fails on `pk_artifact_release_ds`

An upgrade stops in `dashfc08` while publishing an existing shared dashboard:
`duplicate key value violates unique constraint "pk_artifact_release_ds"`.
The migration must preserve every distinct resource attached to that dashboard,
even when its conversation contains repeated data-source associations.

## Root cause (validated)

`backend/alembic/versions/905a08bb2ca7_report_data_source_association.py:24`
creates the legacy association table without a primary key or uniqueness
constraint. Repeated `(report_id, data_source_id)` pairs are valid in that schema.
`backend/alembic/versions/dashfc01_dashboards_first_class_schema.py:88`
requires each `(artifact_release_id, data_source_id)` pair to be unique.
The copy loop at
`backend/alembic/versions/dashfc08_existing_dashboards.py:630` previously inserted
every source row, including duplicates, into that constrained destination.

This affects upgrades that publish a shared conversation with duplicate source
links. It does not mean the data-source entity itself is duplicated. The original
file-link migration already creates a composite primary key
(`backend/alembic/versions/161909d75fe3_files_type.py:31`), so files do not exhibit
the same duplicate-input state on the normal migration chain.

## Loop A — deterministic reproduction

From a fresh checkout with Python 3.13 and backend development dependencies:

```bash
cd backend
uv sync --frozen --extra dev
TESTING=true uv run pytest tests/e2e/dashboards/test_data_migration.py \
  -k preserves_each_distinct_resource_link --db=sqlite -q --disable-warnings
```

The regression uses the existing dashboard corpus, adds three duplicate copies
of each link to a conversation with multiple different sources, then runs the
real Alembic upgrade. It asserts that the release contains exactly the distinct
source IDs and that the original association rows are unchanged. A second case
checks preservation of distinct file links.

Before the migration fix, the data-source case failed at the destination insert:

```text
sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) UNIQUE constraint failed:
artifact_release_data_source_association.artifact_release_id,
artifact_release_data_source_association.data_source_id
```

To reproduce the failure after applying the fix, temporarily remove `.distinct()`
from the resource-link SELECT in `dashfc08`, run the command, then restore it.
No production database or external credentials are needed.

## The fix

Select distinct resource IDs before copying them to a release. This leaves all
legacy rows intact and retains every different resource. Patch `dashfc08`
directly: a later migration cannot repair an upgrade blocked before it reaches
that revision.

Run the regression together with the existing rerun and downgrade contracts:

```bash
TESTING=true uv run pytest tests/e2e/dashboards/test_data_migration.py \
  -k 'preserves_each_distinct_resource_link or running_the_migration_again_adds_nothing or changes_no_existing_row_and_downgrades_exactly' \
  --db=sqlite -q --disable-warnings
```

Repeat with `--db=postgres` for the PostgreSQL leg (Docker required).

Observed after the fix on SQLite: `4 passed, 30 deselected` (178.83s).
Observed on PostgreSQL 15: `4 passed, 30 deselected` (213.24s).

The local Docker disk was full, so the PostgreSQL test server needed a
disposable in-memory data directory. An equivalent setup is:

```bash
docker run -d --name bow-dashfc08-regression \
  --tmpfs /var/lib/postgresql/data:rw,size=1g \
  -e POSTGRES_PASSWORD=regression -e POSTGRES_DB=bow_dashfc08 \
  -p 127.0.0.1::5432 postgres:15 \
  -c fsync=off -c synchronous_commit=off -c full_page_writes=off
docker port bow-dashfc08-regression 5432
```

Set `TEST_DATABASE_URL` to that disposable server's URL using the printed port,
database `bow_dashfc08`, user `postgres`, and test-only password `regression`,
then run the command above with `--db=postgres`. Remove the test container after
verification with `docker rm -f -v bow-dashfc08-regression`.

## What this proves / regression notes

The loop exercises actual migration execution and the release tables, rather
than a mocked insertion. It covers duplicate source links, multiple distinct
resources, file preservation, source-row preservation, idempotence, and downgrade
restoration. It does not inspect or repair the reported production database.
