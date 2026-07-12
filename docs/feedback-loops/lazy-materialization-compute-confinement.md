# Feedback Loop — Lazy materialization and compute confinement

Lazy query ingestion was bounded, but the result handle still exposed several
ways to move the resource spike after ingestion: public materializers could
load the full relation, downstream DuckDB work had no wall-clock or aggregate
spill ceiling, and caller-selected Parquet paths could cross the per-query
filesystem boundary. A second review found constructor-based deletion,
Arrow-to-pandas amplification, and a concurrent temp-budget race.

This loop keeps those boundaries runnable and deterministic. It uses local
DataFrames, Parquet, and DuckDB only; no database server or API credential is
required.

## Root causes

- `LazyFrame.to_df()` / `to_arrow()` and caller-supplied bounded limits were
  not clamped to an operator ceiling. Arrow batches also could expand into
  much larger pandas object columns before pandas memory was measured.
- The query timeout stopped source acquisition, but not later DuckDB
  aggregation or materialization. DuckDB also inherited a filesystem-sized
  temp limit, and concurrent connections could each snapshot and claim the
  same remaining spill capacity.
- `LazyFrame.from_parquet()` let a caller choose an allowed directory and take
  deletion ownership. Blocking that factory alone was insufficient because
  `type(lf)(..., owns_source=True)` could still register an arbitrary path for
  deletion.

## Reproduce and verify

From the repository root:

```bash
cd backend
TESTING=true \
BOW_DATABASE_URL=sqlite:///db/app.db \
MPLCONFIGDIR=/tmp/matplotlib-cache \
.venv/bin/pytest -q --disable-warnings \
  tests/unit/test_lazy_frame.py::test_public_materializers_cannot_exceed_global_ceiling \
  tests/unit/test_lazy_frame.py::test_lazy_compute_timeout_interrupts_blocking_relation \
  tests/unit/test_lazy_frame.py::test_lazy_compute_checks_aggregate_capacity_after_growth \
  tests/unit/test_lazy_frame.py::test_duckdb_temp_directory_has_explicit_remaining_budget \
  tests/unit/test_lazy_frame.py::test_public_from_parquet_cannot_read_or_own_arbitrary_files \
  tests/unit/test_lazy_frame.py::test_public_constructor_cannot_take_file_deletion_ownership \
  tests/unit/test_lazy_frame.py::test_pandas_amplification_is_rejected_before_conversion \
  tests/unit/test_lazy_frame.py::test_live_duckdb_connections_reserve_disjoint_temp_budgets \
  tests/unit/test_lazy_wiring_and_overrides.py::TestRound8MaterializationAndIsolation::test_generated_code_cannot_materialize_past_operator_ceiling \
  tests/unit/test_lazy_wiring_and_overrides.py::TestRound8MaterializationAndIsolation::test_generated_code_cannot_call_parquet_factory \
  tests/unit/test_lazy_wiring_and_overrides.py::TestRound8MaterializationAndIsolation::test_generated_code_cannot_construct_owning_lazyframe \
  tests/unit/test_lazy_wiring_and_overrides.py::TestRound8MaterializationAndIsolation::test_connection_timeout_carries_into_downstream_compute
```

Observed red legs during development:

- Initial boundary suite: `7 failed, 209 warnings in 7.25s`.
- Independent second-pass suite: `4 failed, 209 warnings in 6.05s`.

Observed green legs after the fixes:

- Initial boundary suite: `7 passed, 209 warnings in 7.31s`.
- Timeout propagation case: `1 passed`.
- Independent second-pass suite: `4 passed, 209 warnings in 8.23s`.
- Broad regression gate (`test_lazy_frame.py`, `test_lazy_wiring_and_overrides.py`,
  `test_query_timeout.py`, and `test_sandbox_feedback_loop.py`): `169 passed,
  209 warnings in 152.47s`.

## Fix invariants

- Every public materializer is clamped by
  `BOW_LAZY_RESULT_MATERIALIZE_CAP` and
  `BOW_LAZY_RESULT_MATERIALIZE_MAX_BYTES`; pandas object amplification is
  estimated and rejected before conversion.
- Each downstream compute checks aggregate capacity before and after work and
  carries the connection timeout into DuckDB interruption.
- DuckDB gets an explicit temp maximum. Live connections atomically reserve
  disjoint shares of the spill root, dividing remaining free-space capacity
  across active slots; expected process concurrency is configurable through
  `BOW_LAZY_MAX_CONCURRENT_COMPUTES` (default `8`).
- Only trusted factories receive deletion ownership. Generated code cannot
  call the Parquet factory or use `type(...)` to forge an owning frame.
- Closing a frame remains idempotent and releases both its DuckDB connection
  reservation and private spill directory.
