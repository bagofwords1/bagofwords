# Feedback Loop — lazy query isolation and accounting hardening

The lazy-query PR passed its original focused unit suite while public
`LazyFrame` and wrapper surfaces still allowed sibling-spill reads, unmetered
or incorrectly metered results, lossy rows/types, and leaked spill directories.
This loop fixes the contract at those public boundaries and runs without live
data sources or credentials.

## Root cause (validated)

- `backend/app/data_sources/clients/lazy_frame.py:274` returned an unconfined
  DuckDB connection when a confinement setting failed, and
  `backend/app/data_sources/clients/lazy_frame.py:451` accepted DuckDB `COPY`
  through the public `.sql()` surface.
- The SQLAlchemy helper at
  `backend/app/data_sources/clients/lazy_frame.py:908` bypassed the per-query
  directory allocator, so one live frame could read another frame's spill.
- Normalization, aggregate-cap checks, zero-column row handling, and first-chunk
  null typing in `backend/app/data_sources/clients/lazy_frame.py:876` and
  `backend/app/data_sources/clients/lazy_frame.py:920` did not match the bytes,
  rows, or types that were actually written.
- `backend/app/ai/code_execution/code_execution.py:233` allowed generated code
  to reach wrapper-private attributes, bypassing the lazy opt-in, timeout,
  capture, and quota paths.
- Multi-part metering and derived-frame materialization used incomplete or
  source-level metadata (`backend/app/data_sources/clients/lazy_frame.py:558`
  and `backend/app/data_sources/clients/lazy_frame.py:638`), and the execution
  boundary did not close the owning chain.
- Mongo Decimal128 values were coerced to float in
  `backend/app/data_sources/clients/mongodb_client.py:191`, losing significant
  digits.

## Loop A — deterministic reproduction (no external services)

Run from `backend/`:

```bash
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true .venv/bin/pytest \
  tests/unit/test_lazy_frame.py::test_zero_column_chunk_mid_stream_preserves_rows \
  tests/unit/test_lazy_frame.py::test_sqlalchemy_lazy_frames_cannot_read_sibling_spills \
  tests/unit/test_lazy_frame.py::test_lazy_sql_rejects_copy_even_inside_private_spill_dir \
  tests/unit/test_lazy_frame.py::test_duckdb_confinement_setup_fails_closed \
  tests/unit/test_lazy_frame.py::test_nested_json_is_metered_after_normalization \
  tests/unit/test_lazy_frame.py::test_aggregate_spill_cap_is_enforced_after_growth \
  tests/unit/test_lazy_frame.py::test_all_null_leading_boolean_column_keeps_boolean_semantics \
  tests/unit/test_lazy_frame.py::test_csv_lazy_cap_error_keeps_typed_domain_error \
  tests/unit/test_lazy_frame.py::test_lazy_from_dataframe_write_failure_reclaims_query_dir \
  tests/unit/test_lazy_frame.py::test_lazy_stream_setup_failure_reclaims_query_dir \
  tests/unit/test_lazy_wiring_and_overrides.py::TestRound7SecurityAndAccountingRegressions -q
```

Observed before the fix on pushed commit `bcdeb5a5`:

```text
FFFFFFFFFFFFFFF
15 failed
```

The failures individually demonstrated sibling data disclosure, arbitrary
DuckDB writes, fail-open confinement, private proxy bypass, cap bypasses, row
loss, boolean and Decimal corruption, typed-error wrapping, partial metering,
incorrect derived-result rejection, and orphaned query directories.

## The fix

- Allocate every strategy under a private `q_*` directory, fail closed when
  DuckDB cannot be confined, and accept exactly one read-only `SELECT`/`WITH`
  statement in `LazyFrame.sql()`.
- Reject every private attribute in generated Python, not only a fixed dunder
  list.
- Meter normalized data, check aggregate growth after writes, preserve
  zero-field rows with a hidden Parquet sentinel, and roll null-only schemas
  when the first concrete type arrives.
- Preserve typed query-limit errors through DuckDB-family context managers,
  fail closed on any unreadable spill part, materialize the current derived
  relation in bounded Arrow batches, and close its owning frame chain.
- Preserve Python `Decimal`/BSON Decimal128 through Arrow and use Arrow-to-pandas
  materialization so DuckDB does not coerce decimals to float.
- Reclaim the private query directory on write, stream-setup, or reader-open
  failures.

Re-running Loop A after the fix:

```text
...............
15 passed
```

## What this proves / regression notes

The loop proves the new invariants through public helpers and real local
DuckDB/SQLite/SQLAlchemy drivers. It does not claim that every third-party
connector can stream natively; connectors without a native strategy still use
the bounded materialize-then-spill fallback. Full SQLite/Postgres, AI, and live
integration jobs remain separate CI coverage.
