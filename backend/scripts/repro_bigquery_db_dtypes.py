"""Deterministic repro for the BigQuery `db-dtypes` failure (offline, no live BQ).

Background
----------
Users hit this on a BigQuery data source::

    Execution error: Please install the 'db-dtypes' package to use this function.

`BigQueryClient.execute_query` (and the table-introspection paths) all call
`query_job.result().to_dataframe()`
(``backend/app/data_sources/clients/bigquery_client.py:90,127,172``).

In `google-cloud-bigquery` 3.42.x, `RowIterator.to_dataframe()` calls
`_pandas_helpers.verify_pandas_imports()` *unconditionally at the top*
(``table.py:2433``), which raises::

    ValueError("Please install the 'db-dtypes' package to use this function.")

whenever `db_dtypes` is not importable — i.e. for *every* BigQuery query, not
only date/numeric results. So the fix is purely a dependency one: ship
`db-dtypes`.

This script exercises the exact same public `to_dataframe()` guard the app hits
(via `_EmptyRowIterator`, a real google-cloud-bigquery class), so it reproduces
the user-facing error with no network and no credentials.

Exit codes
----------
* ``0`` — `to_dataframe()` succeeded (db-dtypes present)  -> FIX VERIFIED
* ``1`` — got the exact db-dtypes ValueError              -> BUG REPRODUCED
* ``2`` — some other/unexpected outcome
"""
from __future__ import annotations

import sys

_NO_DB_TYPES_ERROR = "Please install the 'db-dtypes' package to use this function."


def main() -> int:
    try:
        import db_dtypes  # noqa: F401

        db_dtypes_present = True
    except Exception as exc:  # pragma: no cover - depends on env
        db_dtypes_present = False
        print(f"[env] db_dtypes import FAILED: {exc!r}")
    else:
        print("[env] db_dtypes import OK")

    from google.cloud.bigquery.table import _EmptyRowIterator

    # Real public API the app relies on: RowIterator/_EmptyRowIterator.to_dataframe().
    # Both call _pandas_helpers.verify_pandas_imports() first, which is the guard
    # that raises the db-dtypes error when the package is missing.
    try:
        df = _EmptyRowIterator().to_dataframe()
    except ValueError as exc:
        if str(exc) == _NO_DB_TYPES_ERROR:
            print(f"[repro] BUG REPRODUCED: to_dataframe() raised: {exc}")
            assert not db_dtypes_present, (
                "Got the db-dtypes error even though db_dtypes imported — "
                "unexpected; investigate."
            )
            return 1
        print(f"[repro] UNEXPECTED ValueError: {exc!r}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[repro] UNEXPECTED error: {exc!r}")
        return 2

    print(f"[repro] FIX VERIFIED: to_dataframe() returned a {type(df).__name__} "
          f"(rows={len(df)}) with no db-dtypes error")
    assert db_dtypes_present, "to_dataframe() succeeded but db_dtypes not importable?"
    return 0


if __name__ == "__main__":
    sys.exit(main())
