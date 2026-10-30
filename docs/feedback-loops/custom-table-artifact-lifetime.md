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
  kernel lock via the pinned `filelock` dependency serializes refreshes and
  maintenance across workers sharing the volume. A queued refresh waits its
  turn with exponential backoff (as the in-process lock it replaced did), so a
  sibling table's scheduled tick is run after the current scan rather than
  dropped; a bounded wait (the existing localized 409) is available but no
  product path uses it. Maintenance skips busy connections. Lock files are
  never unlinked, process exit releases locks, and soft-lock fallback is
  disabled.
- `backend/app/services/custom_query_service.py:652`: reload the table under the
  lock before choosing the old path; skip a table deleted while queued. Keep the
  previous file until a successful swap. On an ambiguous failed commit, delete
  the replacement only after rollback and a fresh query prove it unreferenced.
  If verification fails, leave it for GC. Cleanup after a successful swap runs
  even when activation sync fails. Cancellation waits for extraction to finish
  before removing its unpublished output and releasing the lock, even after
  repeated cancellation; best-effort terminal status prevents stale `running`.
  Cancellation also sets an abort flag the extractor polls between batches,
  so the source stops within one batch instead of the full scan, and the row
  records a cancellation reason rather than a blank error.
- Delete and update take no volume lock, so neither waits behind another
  table's refresh. The reference swap is a conditional `UPDATE ... WHERE
  deleted_at IS NULL`: a refresh that loses to a concurrent delete discards
  its new file instead of resurrecting the row. Create and update reload the
  row before touching the shared job store, and `scheduled_refresh` claims
  each tick through `claim_scheduled_run` so only one worker scans the source.
- The delete path commits its cleared reference before removing the file, so
  a failed delete commit preserves the original artifact.
- `backend/app/services/fast_artifact_maintenance.py`: daily cleanup reads all
  DB references once (including soft-deleted rows) and identifies each by
  (connection directory, file name) rather than resolved absolute path, so a
  volume reached from a different mount point or install directory cannot make
  every served artifact look unreferenced. Each directory's lock-and-prune runs
  in a worker thread, off the event loop. It removes only unreferenced `.db`,
  `.tmp`, and `.wal` files older than 24h. The cutoff is captured before the snapshot: any
  newly committed reference points at a fresh UUID file, protected by its age.
  It preserves referenced WALs, recent files, symlinks and unknown file types;
  failed DB reads abort the sweep safely. Per-file OS errors are logged without
  preventing other deletions or discarding their totals.
- `backend/main.py`: registration at 03:15 server time; the scheduled wrapper
  claims the shared job before sweeping, preventing duplicate worker execution.
  A restored scheduler/next deployment runs this automatically; no migration.

Observed before PR creation: 9 lifecycle tests passed on SQLite and PostgreSQL,
and 67 targeted tests passed. After addressing review comments:

```text
SQLite sandbox:                   Ran 13 tests — OK
Targeted pytest --db=sqlite:       71 passed
Lifecycle pytest --db=postgres:   13 passed
```

After the second review round:

```text
Lifecycle + neighbours --db=sqlite:   139 passed
Lifecycle --db=external (PG 16):       18 passed
Custom-table e2e routes --db=sqlite:  225 passed
```

The normal-suite command is:

```sh
uv run pytest tests/unit/test_fast_artifact_lifecycle.py \
  tests/unit/test_custom_queries.py tests/unit/test_custom_query_permissions.py -q
```

For PostgreSQL, point **only** at a disposable server: the harness drops and
recreates that database's `public` schema before every test.

```sh
ARTIFACT_LOOP_POSTGRES_URL=postgresql://postgres@127.0.0.1:55483/bow_artifact_test \
  uv run python ../tools/agent/custom_table_artifact_loop.py
```

The observed PostgreSQL run used a temporary local PostgreSQL 14 server; a Docker
PostgreSQL 16 attempt could not initialize because the local Docker VM had no
free disk. Its temporary container/volume were removed. Production was untouched.

## What this proves / limits

The eighteen regressions cover stale sessions, cross-process exclusion/crash release,
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

## Review follow-up — PR #1505

Three additional checks were run against the original PR implementation before
repair: an unremovable file, repeated request cancellation, and a stale update
rescheduling a deleted table. All three failed; all pass with the review fixes.
Additional tests verify terminal cancelled status, bounded lock conflict/reuse,
and real shared-job deduplication against the migration's unique constraint.

The module is now plain pytest on the harness database (`pytestmark = db`): a
fresh, migrated schema per test on whichever `--db` CI selects, no second engine
and no `create_all`. Destructive fault injection (triggers, dropped tables) runs
on that per-test database; triggers are removed in `finally` because they sit
outside the harness's schema fingerprint. The claim regression uses the real
`scheduled_job_runs` table from the migration chain with only the clock pinned.
The e2e workflow runs the module in the same pytest invocation as the e2e suite
on both backends, sharing that job's one Postgres server.

Second review round: 18 tests pass on SQLite and PostgreSQL 16 (`--db=external`);
reverting the GC identity, the scheduled-refresh claim, or the lock-free delete
each makes its test fail.
