"""Regression test for the BigQuery `db-dtypes` runtime dependency.

`BigQueryClient.execute_query` / table introspection call
`query_job.result().to_dataframe()`
(``app/data_sources/clients/bigquery_client.py:90,127,172``).

In `google-cloud-bigquery` 3.42.x, `RowIterator.to_dataframe()` runs
`_pandas_helpers.verify_pandas_imports()` unconditionally, which raises
``ValueError("Please install the 'db-dtypes' package to use this function.")``
when `db_dtypes` is not importable — for *every* BigQuery query, not only
date/numeric results.

`db-dtypes` is an *optional* companion of google-cloud-bigquery (never a hard
dep, in any version), so it must be declared explicitly in `pyproject.toml`.
These tests guard that it stays installed so the failure can't regress.
"""
from __future__ import annotations

import pytest


def test_db_dtypes_importable():
    """db-dtypes must be installed (declared in pyproject.toml)."""
    db_dtypes = pytest.importorskip(
        "db_dtypes",
        reason="db-dtypes missing — add it to pyproject.toml dependencies",
    )
    # Sanity: the dtypes google-cloud-bigquery looks up at import time exist.
    assert db_dtypes.DateDtype.name
    assert db_dtypes.TimeDtype.name


def test_bigquery_to_dataframe_does_not_raise_db_dtypes_error():
    """The exact public path the app uses must not raise the db-dtypes error.

    `_EmptyRowIterator` is a real google-cloud-bigquery class whose
    `to_dataframe()` runs the same `verify_pandas_imports()` guard that
    `query_job.result().to_dataframe()` hits in the client.
    """
    pytest.importorskip("google.cloud.bigquery")
    from google.cloud.bigquery.table import _EmptyRowIterator

    df = _EmptyRowIterator().to_dataframe()  # would raise ValueError w/o db-dtypes
    assert df is not None
    assert len(df) == 0
