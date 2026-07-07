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


# ── Indexing progress + cancellation ────────────────────────────────────────
# These guard the fix for "the progress bar finishes in 3s but indexing takes
# 40 minutes": the QVD→Parquet convert (the real work) now reports row-level
# progress through awarm_all and can be stopped mid-flight.

_FIXTURE_ROWS = 120  # test_source.qvd carries 120 records (see header NoOfRecords)


@pytest.fixture
def fresh_client(tmp_path, monkeypatch) -> QVDClient:
    """A QVDClient with an isolated (cold) cache — awarm_all will really convert."""
    monkeypatch.setattr(qvd_client, "_QVD2PARQUET_BIN", _BIN)
    monkeypatch.setattr(qvd_client, "_CACHE_DIR", tmp_path / "qvd_cache")
    return QVDClient(file_paths=str(_FIXTURE))


def test_record_count_from_header():
    assert QVDClient._read_qvd_record_count(str(_FIXTURE)) == _FIXTURE_ROWS


def test_warm_reports_row_level_converting_progress(fresh_client):
    """awarm_all drives a 'converting' phase whose done/total are ROWS, ending
    at the file's true row count — so a long convert moves the bar instead of
    the schema-only header read that used to finish instantly."""
    seen: list[tuple] = []

    def cb(phase, item, done, total):
        seen.append((phase, done, total))

    paths = asyncio.run(fresh_client.awarm_all(progress_callback=cb))
    assert len(paths) == 1

    converting = [e for e in seen if e[0] == "converting"]
    assert converting, "expected a 'converting' phase to be reported"
    # Total is the header row count, not the file count.
    assert converting[-1][2] == _FIXTURE_ROWS
    # Progress ends at 100% and never regresses.
    dones = [d for _, d, _ in converting]
    assert dones == sorted(dones), f"progress regressed: {dones}"
    assert dones[-1] == _FIXTURE_ROWS


def test_warm_honors_cancel_and_writes_no_parquet(fresh_client):
    """A cancel request aborts warming and leaves no half-written cache."""
    from app.data_sources.clients.progress import IndexingCancelled

    with pytest.raises(IndexingCancelled):
        asyncio.run(fresh_client.awarm_all(cancel_check=lambda: True))

    cache_dir = qvd_client._CACHE_DIR
    leftovers = list(cache_dir.glob("*.parquet")) if cache_dir.exists() else []
    assert not leftovers, f"cancelled warm left cache files: {leftovers}"


def test_index_stats_reports_source_size(fresh_client):
    stats = fresh_client.index_stats()
    assert stats["file_count"] == 1
    assert stats["row_count"] == _FIXTURE_ROWS
    assert stats["source_bytes"] > 0


def test_convert_streaming_cancels_midway(tmp_path, monkeypatch):
    """The subprocess-streaming convert polls cancel_check while the child is
    running and kills it — validated with a stand-in binary that emits progress
    slowly so the cancel lands mid-convert."""
    from app.data_sources.clients.progress import IndexingCancelled

    fake_bin = tmp_path / "slow_qvd2parquet.sh"
    fake_bin.write_text(
        "#!/usr/bin/env bash\n"
        "out=\"$2\"\n"
        "echo 'qvd2parquet: progress 0 100' 1>&2\n"
        "for i in 1 2 3 4 5 6 7 8 9 10; do\n"
        "  echo \"qvd2parquet: progress $((i*10)) 100\" 1>&2\n"
        "  sleep 0.5\n"
        "done\n"
        "echo done > \"$out\"\n"
    )
    fake_bin.chmod(0o755)
    monkeypatch.setattr(qvd_client, "_QVD2PARQUET_BIN", str(fake_bin))

    c = QVDClient(file_paths=str(_FIXTURE))
    calls = {"n": 0}

    def cancel_after_a_bit() -> bool:
        calls["n"] += 1
        return calls["n"] > 3  # let a few progress lines through, then cancel

    out = tmp_path / "out.parquet"
    with pytest.raises(IndexingCancelled):
        c._run_convert_streaming(
            str(_FIXTURE), out, cancel_check=cancel_after_a_bit,
        )
    assert not out.exists(), "cancelled convert must not leave output"
