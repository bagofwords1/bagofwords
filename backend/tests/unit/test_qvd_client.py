"""End-to-end test for QVDClient temporal handling.

Drives the real public API (warm → schema → query) against a committed QVD
fixture, exercising the standalone ``qvd2parquet`` Rust converter. The fixture
``test_source.qvd`` carries ``$timestamp`` columns (``Date``,
``PromisedDeliveryDate``) plus a string-stored ``InvoiceDate`` (tagged
``$ascii``/``$text``), so it doubles as a regression test that:

  * Qlik dual date/timestamp serials become real ``TIMESTAMP`` values rather
    than the raw Excel-style serial numbers (the bug this guards against), and
  * a date-named-but-text column is *not* coerced to a temporal type.

Skips cleanly when the ``qvd2parquet`` binary hasn't been built — the converter
lives in ``tools/qvd2parquet`` and is normally deployed to the runtime image.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pandas as pd
import pytest

import app.data_sources.clients.qvd_client as qvd_client
from app.data_sources.clients.qvd_client import QVDClient

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE = Path(__file__).resolve().parents[1] / "config" / "test_source.qvd"
_BUILT_BIN = _REPO_ROOT / "tools" / "qvd2parquet" / "target" / "release" / "qvd2parquet"


def _find_binary() -> str | None:
    """Locate qvd2parquet: explicit override, repo build dir, then PATH."""
    override = os.environ.get("QVD2PARQUET_BIN")
    if override and os.path.isfile(override):
        return override
    if _BUILT_BIN.is_file():
        return str(_BUILT_BIN)
    return shutil.which("qvd2parquet")


_BIN = _find_binary()

pytestmark = pytest.mark.skipif(
    _BIN is None,
    reason="qvd2parquet binary not built (run `cargo build --release` in tools/qvd2parquet)",
)


@pytest.fixture
def client(tmp_path, monkeypatch) -> QVDClient:
    """A QVDClient pointed at the fixture, with an isolated parquet cache and
    the converter binary resolved to whatever we found above."""
    monkeypatch.setattr(qvd_client, "_QVD2PARQUET_BIN", _BIN)
    monkeypatch.setattr(qvd_client, "_CACHE_DIR", tmp_path / "qvd_cache")
    c = QVDClient(file_paths=str(_FIXTURE))
    # Populate the parquet cache via the real Rust converter.
    asyncio.run(c.awarm_all())
    return c


def _columns(client: QVDClient) -> dict[str, str]:
    tables = client.get_tables()
    assert len(tables) == 1, "fixture should expose exactly one table"
    return {col.name: col.dtype for col in tables[0].columns}


def test_timestamp_columns_have_temporal_schema(client):
    cols = _columns(client)
    assert cols["Date"] == "TIMESTAMP"
    assert cols["PromisedDeliveryDate"] == "TIMESTAMP"


def test_text_stored_date_is_not_coerced(client):
    # InvoiceDate is tagged $ascii/$text — it must stay a string, not become a date.
    assert _columns(client)["InvoiceDate"] == "VARCHAR"


def test_numeric_columns_unaffected(client):
    cols = _columns(client)
    assert cols["OrderNumber"] == "BIGINT"
    assert cols["BackOrder"] == "DOUBLE"


def test_query_returns_real_dates_not_serials(client):
    df = client.execute_query(
        'SELECT "Date" FROM test_source WHERE "Date" IS NOT NULL LIMIT 10'
    )
    assert not df.empty
    # The bug returned Excel serials (~40000); a correct conversion yields real
    # datetimes. Assert the column is datetime-typed and the years are plausible.
    assert pd.api.types.is_datetime64_any_dtype(df["Date"])
    years = df["Date"].dt.year
    assert years.between(1990, 2050).all(), f"unexpected years: {sorted(set(years))}"
