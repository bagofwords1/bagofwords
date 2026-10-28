# Feedback Loop — custom-table refreshes leave thousands of DuckDB files

Validate that replacing a custom table retains exactly its current encrypted
artifact, and that maintenance reclaims abandoned files without deleting live
references or in-progress work. The operational investigation found 4,094 old,
unreferenced files (~22 GiB) on `bow`; those were cleaned separately. This PR
changes code only and does not deploy to any customer instance.

## Root cause (validated)

At baseline `78d61ab3f`, `backend/app/services/custom_query_service.py:649` used
only an in-process asyncio lock; line 657 read `cq.artifact_path` from a possibly
stale ORM object. Two sessions loaded before either refresh therefore both saw
the same old path. Even sequential refreshes left the first session's newly
created file orphaned. The regression reproduced this with real SQLAlchemy
sessions, SQLite source extraction, encrypted DuckDB files and actual queries.
It proves a leak mechanism, not which historical refresh generated each file on
`bow`.

Additional lifecycle gaps: failed reference swaps did not discard their new
artifact, activation-sync failure skipped old-file cleanup, request cancellation
could release coordination while its extraction thread kept writing, and there
was no orphan sweep. `artifacts.delete_artifact()` also left WAL sidecars behind.

## Loop A — deterministic isolated reproduction

Run from the repository's backend with Python 3.13 and its normal dependencies:

```sh
cd backend
uv sync --frozen --extra dev
uv run python ../tools/agent/custom_table_artifact_loop.py
```

The runner sets temporary database URLs **before importing the app**, never
starts its lifespan, and runs `tests/unit/test_fast_artifact_lifecycle.py` against
a fresh private database and artifact root for each test. The source is a real,
small local SQLite database. No LLM, OAuth, customer data or credentials are used.
DDL triggers and dropped tables model real storage failures; a connector I/O
barrier models cancellation; a second interpreter tests process exclusion and
crash release without timing sleeps.

To observe the original failure with this regression, use a disposable checkout
at baseline `78d61ab3f`, copy the test module and runner from this PR into the same
paths, and run:

```sh
uv run python ../tools/agent/custom_table_artifact_loop.py \
  ArtifactLifecycle.test_refreshes_with_preloaded_sessions_keep_only_current_artifact
```

Observed before (also rechecked by temporarily restoring only the baseline
service implementation in the isolated worktree):

```text
AssertionError: Items in the first set but not the second:
PosixPath('.../fast/<connection>/<unreferenced-file>.db')
Ran 1 test
FAILED (failures=1)
```

## The fix

- `backend/app/data_sources/fast/artifacts.py:115`: a persistent per-connection
  `flock` on the artifact volume coordinates refresh, delete and maintenance
  across workers sharing those files. Waiting is asynchronous; maintenance skips
  busy connections. Lock files are never unlinked, and process exit releases locks.
- `backend/app/services/custom_query_service.py:652`: reload the table under the
  lock before choosing the old path; skip a table deleted while queued. Keep the
  previous file until a successful swap. On an ambiguous failed commit, delete
  the replacement only after rollback and a fresh query prove it unreferenced.
  If verification fails, leave it for GC. Cleanup after a successful swap runs
  even when activation sync fails. Cancellation waits for extraction to finish
  before removing its unpublished output and releasing the lock.
- The delete path now commits its cleared reference before removing the file,
  so a failed delete commit preserves the original artifact.
- `backend/app/services/fast_artifact_maintenance.py:14`: daily cleanup acquires
  the same volume lock, reads all DB references (including soft-deleted rows),
  and removes only unreferenced `.db`, `.tmp`, and `.wal` files older than 24h.
  It preserves referenced WALs, recent files, symlinks and unknown file types;
  failed DB reads preserve every file in that directory.
- `backend/main.py`: scheduler-leader registration at 03:15 server time.
  A restored scheduler/next deployment runs this automatically; no migration.

Observed after:

```text
SQLite sandbox:     Ran 9 tests — OK
PostgreSQL sandbox: Ran 9 tests — OK
Targeted pytest:    67 passed
```

The normal-suite command is:

```sh
uv run pytest tests/unit/test_fast_artifact_lifecycle.py \
  tests/unit/test_custom_queries.py tests/unit/test_custom_query_permissions.py -q
```

For PostgreSQL, point **only** at a disposable server with database-create
permission (the runner creates and drops uniquely named databases):

```sh
ARTIFACT_LOOP_POSTGRES_URL=postgresql://postgres@127.0.0.1:55483/postgres \
  uv run python ../tools/agent/custom_table_artifact_loop.py
```

The observed PostgreSQL run used a temporary local PostgreSQL 14 server; a Docker
PostgreSQL 16 attempt could not initialize because the local Docker VM had no
free disk. Its temporary container/volume were removed. Production was untouched.

## What this proves / limits

The nine regressions cover stale sessions, cross-process exclusion/crash release,
source failure, failed refresh/delete commits, activation-sync failure,
cancellation, queued refresh after deletion, and safe/idempotent orphan cleanup
(including missing DB schema, recent files, active refresh, referenced WALs,
soft-deleted references, orphaned connection directories and symlinks).

All final targeted tests passed; there were no remaining test failures to
attribute away. During development, extending the failed-swap test to perform a
second action initially reused a Connection expired by rollback; explicitly
reloading that test object fixed the test setup. No product failure was dismissed
as pre-existing.
The full backend suite, remote source integrations, network filesystems and a
production deployment were not exercised. The locking contract requires an
artifact volume supporting `flock`; replicas sharing artifacts must share that
volume. A 24h grace period protects pre-lock writers during a rolling deployment
(the normal extraction wall-clock cap is 30 minutes). GC cannot repair already
missing referenced files and intentionally preserves soft-deleted references.
