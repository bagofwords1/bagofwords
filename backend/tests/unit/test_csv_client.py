"""End-to-end test for CSVClient.

Drives the real public API (schema → query) against a committed CSV fixture.
Unlike QVD, CSV needs no external converter — DuckDB reads it natively — so this
test always runs (no skip guard).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.data_sources.clients.csv_client import CSVClient

_FIXTURE = Path(__file__).resolve().parents[1] / "config" / "test_source.csv"


def _columns(client: CSVClient) -> dict[str, str]:
    tables = client.get_tables()
    assert len(tables) == 1, "fixture should expose exactly one table"
    assert tables[0].name == "test_source"
    return {col.name: col.dtype for col in tables[0].columns}


def test_schema_infers_types():
    cols = _columns(CSVClient(file_paths=str(_FIXTURE)))
    assert cols["OrderNumber"] == "BIGINT"
    assert cols["Product"] == "VARCHAR"
    assert cols["Amount"] == "DOUBLE"
    # DuckDB auto-detects ISO dates.
    assert cols["OrderDate"] == "DATE"


def test_query_returns_rows():
    client = CSVClient(file_paths=str(_FIXTURE))
    df = client.execute_query("SELECT * FROM test_source ORDER BY OrderNumber")
    assert len(df) == 5
    assert list(df["Product"]) == ["Widget", "Gadget", "Widget", "Gizmo", "Gadget"]


def test_query_aggregates():
    client = CSVClient(file_paths=str(_FIXTURE))
    df = client.execute_query(
        "SELECT Product, SUM(Amount) AS total FROM test_source GROUP BY Product ORDER BY Product"
    )
    totals = dict(zip(df["Product"], df["total"]))
    assert totals["Widget"] == pytest.approx(39.98)
    assert totals["Gadget"] == pytest.approx(99.00)


def test_query_returns_real_dates():
    client = CSVClient(file_paths=str(_FIXTURE))
    df = client.execute_query("SELECT OrderDate FROM test_source LIMIT 5")
    assert not df.empty
    assert pd.api.types.is_datetime64_any_dtype(df["OrderDate"])


def test_test_connection_ok():
    result = CSVClient(file_paths=str(_FIXTURE)).test_connection()
    assert result["success"] is True
    assert result["details"]["files_found"] == 1


def test_test_connection_no_files(tmp_path):
    result = CSVClient(file_paths=str(tmp_path / "*.csv")).test_connection()
    assert result["success"] is False
    assert result["details"]["files_found"] == 0
