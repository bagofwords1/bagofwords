"""Unit tests for the file-based PBIX connector (PBIXClient + pbix_common).

Covers:
- file/glob pattern resolution
- test_connection framing (no files, bad zip, missing DataModel)
- DuckDB view naming (bare when unique, file-prefixed on cross-file duplicates)
- get_tables schema building from a mocked model (columns, fks, measures)
- extract_pbix_model / materialize_pbix_parquets against a faked pbixray,
  including the skip-and-continue path for tables that fail to decode
- execute_query over a prefab Parquet cache, incl. stale-cache fallback
- awarm_all warm/failure accounting
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import zipfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

import app.data_sources.clients.pbix_client as pbix_client_mod
from app.data_sources.clients.pbix_client import PBIXClient
from app.data_sources.clients.pbix_common import (
    AUTO_DATE_TABLE_RE,
    dax_to_dtype,
    extract_pbix_model,
    materialize_pbix_parquets,
    materialize_pbix_parquets_isolated,
    safe_view_name,
)

# ---------- helpers ---------- #


def _write_pbix(path: Path, with_model: bool = True) -> None:
    """Minimal zip that satisfies the .pbix container checks."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Report/Layout", "{}")
        if with_model:
            zf.writestr("DataModel", b"\x00\x01")


class _FakePBIXRay:
    """Stands in for pbixray.PBIXRay with a fixed two-table model."""

    schema_rows = [
        {"TableName": "Sales", "ColumnName": "OrderID", "PandasDataType": "Int64"},
        {"TableName": "Sales", "ColumnName": "Amount", "PandasDataType": "Float64"},
        {"TableName": "Sales", "ColumnName": "CustomerID", "PandasDataType": "Int64"},
        {"TableName": "Customers", "ColumnName": "CustomerID", "PandasDataType": "Int64"},
        {"TableName": "Customers", "ColumnName": "Name", "PandasDataType": "string"},
        # Auto-generated date table must be filtered everywhere.
        {"TableName": "LocalDateTable_0eafa36c-5fc2-4b6f-84df-278a77695bc4",
         "ColumnName": "Date", "PandasDataType": "datetime64[ns]"},
    ]

    def __init__(self, path):
        self.path = path

    @property
    def schema(self):
        return pd.DataFrame(self.schema_rows)

    @property
    def relationships(self):
        return pd.DataFrame([
            {"FromTableName": "Sales", "FromColumnName": "CustomerID",
             "ToTableName": "Customers", "ToColumnName": "CustomerID",
             "IsActive": True, "Cardinality": "M:1"},
        ])

    @property
    def dax_measures(self):
        return pd.DataFrame([
            {"TableName": "Sales", "Name": "Total Amount",
             "Expression": "SUM(Sales[Amount])", "DisplayFolder": "", "Description": ""},
        ])

    def get_table(self, name):
        if name == "Sales":
            return pd.DataFrame({
                "OrderID": [1, 2, 3], "Amount": [10.0, 20.0, 30.0],
                "CustomerID": [1, 1, 2],
            })
        if name == "Customers":
            raise ValueError("dictionary decode failed")  # the "District" case
        raise KeyError(name)


@pytest.fixture()
def fake_pbixray(monkeypatch):
    mod = types.ModuleType("pbixray")
    mod.PBIXRay = _FakePBIXRay
    monkeypatch.setitem(sys.modules, "pbixray", mod)
    return mod


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "pbix_cache"
    monkeypatch.setattr(pbix_client_mod, "_CACHE_DIR", d)
    return d


# ---------- pbix_common ---------- #


class TestCommonHelpers:
    def test_safe_view_name(self):
        assert safe_view_name("Sales Data") == "Sales_Data"
        assert safe_view_name("9lives") == "t_9lives"
        assert safe_view_name("!!!") == "tbl"

    def test_dax_to_dtype(self):
        assert dax_to_dtype("Int64") == "int"
        assert dax_to_dtype("Float64") == "float"
        assert dax_to_dtype("datetime64[ns]") == "datetime"
        assert dax_to_dtype("string") == "string"
        assert dax_to_dtype(None) == "unknown"

    def test_auto_date_re(self):
        assert AUTO_DATE_TABLE_RE.match("LocalDateTable_0eafa36c-5fc2-4b6f-84df-278a77695bc4")
        assert AUTO_DATE_TABLE_RE.match("DateTableTemplate_ca45d427-b349-4299-a604-253b0d3519f7")
        assert not AUTO_DATE_TABLE_RE.match("Sales")


class TestExtractModel:
    def test_extract(self, fake_pbixray, tmp_path):
        model = extract_pbix_model(str(tmp_path / "x.pbix"))
        names = {t["name"] for t in model["tables"]}
        assert names == {"Sales", "Customers"}  # date table filtered
        sales = next(t for t in model["tables"] if t["name"] == "Sales")
        assert [c["name"] for c in sales["columns"]] == ["OrderID", "Amount", "CustomerID"]
        assert sales["measures"][0]["name"] == "Total Amount"
        assert model["relationships"] == [{
            "from_table": "Sales", "from_column": "CustomerID",
            "to_table": "Customers", "to_column": "CustomerID",
            "is_active": True, "cardinality": "M:1",
        }]


class TestMaterialize:
    def test_skips_failing_table(self, fake_pbixray, tmp_path):
        out = tmp_path / "cache"
        manifest = materialize_pbix_parquets(str(tmp_path / "x.pbix"), out)
        # Customers raises in get_table — skipped, not fatal.
        assert list(manifest.keys()) == ["Sales"]
        assert (out / manifest["Sales"]).exists()
        assert json.loads((out / "manifest.json").read_text()) == manifest

    def test_row_cap_skip(self, fake_pbixray, tmp_path):
        with pytest.raises(RuntimeError, match="no materializable tables"):
            materialize_pbix_parquets(
                str(tmp_path / "x.pbix"), tmp_path / "cache2", max_rows_per_table=1,
            )


# ---------- PBIXClient ---------- #


class TestResolveFiles:
    def test_globs_and_dedup(self, tmp_path):
        _write_pbix(tmp_path / "a.pbix")
        _write_pbix(tmp_path / "b.pbix")
        (tmp_path / "c.txt").write_text("nope")
        c = PBIXClient(f"{tmp_path}/*.pbix\n{tmp_path}/a.pbix")
        assert [os.path.basename(f) for f in c._resolve_files()] == ["a.pbix", "b.pbix"]


class TestTestConnection:
    def test_no_files(self, tmp_path):
        r = PBIXClient(f"{tmp_path}/*.pbix").test_connection()
        assert r["success"] is False
        assert "No .pbix files" in r["message"]

    def test_valid_file(self, tmp_path, cache_dir):
        _write_pbix(tmp_path / "ok.pbix")
        r = PBIXClient(f"{tmp_path}/ok.pbix").test_connection()
        assert r["success"] is True
        assert r["details"]["files_found"] == 1
        assert r["details"]["no_embedded_model"] == []

    def test_bad_zip(self, tmp_path):
        (tmp_path / "bad.pbix").write_bytes(b"not a zip at all")
        r = PBIXClient(f"{tmp_path}/bad.pbix").test_connection()
        assert r["success"] is False
        assert "not a ZIP" in r["message"]

    def test_no_datamodel_warns(self, tmp_path, cache_dir):
        _write_pbix(tmp_path / "live.pbix", with_model=False)
        r = PBIXClient(f"{tmp_path}/live.pbix").test_connection()
        assert r["success"] is True
        assert "no embedded data model" in r["message"]
        assert r["details"]["no_embedded_model"] == ["live.pbix"]


class TestViewNames:
    def test_unique_names_stay_bare(self, tmp_path):
        c = PBIXClient("")
        views = c._view_names({"/d/sales.pbix": ["Orders", "Customers"]})
        assert views == {
            ("/d/sales.pbix", "Orders"): "Orders",
            ("/d/sales.pbix", "Customers"): "Customers",
        }

    def test_cross_file_duplicates_get_prefixed(self):
        c = PBIXClient("")
        views = c._view_names({
            "/d/eu.pbix": ["Orders", "Fx"],
            "/d/us.pbix": ["Orders"],
        })
        assert views[("/d/eu.pbix", "Orders")] == "eu_Orders"
        assert views[("/d/us.pbix", "Orders")] == "us_Orders"
        assert views[("/d/eu.pbix", "Fx")] == "Fx"

    def test_sanitized_collision_suffix(self):
        c = PBIXClient("")
        views = c._view_names({"/d/one.pbix": ["My Table", "My@Table"]})
        assert set(views.values()) == {"one_My_Table", "one_My_Table_2"}


class TestGetTables:
    def test_schema_from_model(self, fake_pbixray, tmp_path, cache_dir):
        _write_pbix(tmp_path / "sales.pbix")
        c = PBIXClient(f"{tmp_path}/sales.pbix")
        tables = c.get_tables()
        by_name = {t.name: t for t in tables}
        assert set(by_name) == {"Sales", "Customers"}

        sales = by_name["Sales"]
        assert [col.name for col in sales.columns] == ["OrderID", "Amount", "CustomerID"]
        assert [col.dtype for col in sales.columns] == ["int", "float", "int"]
        assert len(sales.fks) == 1
        assert sales.fks[0].references_name == "Customers"
        meta = sales.metadata_json["pbix"]
        assert meta["model_table"] == "Sales"
        assert meta["measures"][0]["name"] == "Total Amount"
        assert meta["materialized"] is False  # nothing warmed yet

    def test_schema_json_cached(self, fake_pbixray, tmp_path, cache_dir):
        _write_pbix(tmp_path / "sales.pbix")
        c = PBIXClient(f"{tmp_path}/sales.pbix")
        c.get_tables()
        _, cdir = c._cache_key(str(tmp_path / "sales.pbix"))
        assert (cdir / "schema.json").exists()
        # Second call must not need pbixray at all.
        with patch.object(pbix_client_mod, "extract_pbix_model",
                          side_effect=AssertionError("should not re-extract")):
            tables = c.get_tables()
        assert {t.name for t in tables} == {"Sales", "Customers"}


class TestQueryPath:
    def _prefab_cache(self, c: PBIXClient, filepath: str, tables: dict) -> Path:
        _, cdir = c._cache_key(filepath)
        cdir.mkdir(parents=True, exist_ok=True)
        manifest = {}
        for tname, df in tables.items():
            fname = f"{tname.lower()}.parquet"
            df.to_parquet(cdir / fname, index=False)
            manifest[tname] = fname
        (cdir / "manifest.json").write_text(json.dumps(manifest))
        return cdir

    def test_execute_query_joins(self, tmp_path, cache_dir):
        f = tmp_path / "m.pbix"
        _write_pbix(f)
        c = PBIXClient(str(f))
        self._prefab_cache(c, str(f), {
            "Sales": pd.DataFrame({"CustomerID": [1, 1, 2], "Amount": [10.0, 20.0, 5.0]}),
            "Customers": pd.DataFrame({"CustomerID": [1, 2], "Name": ["Acme", "Bob"]}),
        })
        df = c.execute_query(
            'SELECT cu."Name", SUM(s."Amount") AS total FROM Sales s '
            'JOIN Customers cu ON s."CustomerID" = cu."CustomerID" '
            'GROUP BY 1 ORDER BY 2 DESC'
        )
        assert df.iloc[0]["Name"] == "Acme"
        assert df.iloc[0]["total"] == 30.0

    def test_stale_cache_served_after_edit(self, tmp_path, cache_dir):
        f = tmp_path / "m.pbix"
        _write_pbix(f)
        c = PBIXClient(str(f))
        self._prefab_cache(c, str(f), {"Sales": pd.DataFrame({"A": [1]})})
        # Simulate an edit: mtime moves, fresh cache key misses.
        os.utime(f, ns=(os.stat(f).st_mtime_ns + 10**9, os.stat(f).st_mtime_ns + 10**9))
        with patch.object(pbix_client_mod, "materialize_pbix_parquets",
                          side_effect=AssertionError("must serve stale, not re-parse")):
            df = c.execute_query("SELECT * FROM Sales")
        assert len(df) == 1

    def test_cold_cache_materializes_lazily(self, fake_pbixray, tmp_path, cache_dir, monkeypatch):
        # In-process extract: the faked pbixray module can't cross a subprocess.
        monkeypatch.setenv("BOW_PBIX_INPROCESS_EXTRACT", "1")
        f = tmp_path / "m.pbix"
        _write_pbix(f)
        c = PBIXClient(str(f))
        df = c.execute_query('SELECT SUM("Amount") AS s FROM Sales')
        assert df.iloc[0]["s"] == 60.0


_SHIM_PBIXRAY = '''
import os, signal
import pandas as pd

class PBIXRay:
    """PYTHONPATH shim standing in for pbixray inside the worker subprocess."""
    def __init__(self, path):
        self.path = path

    @property
    def schema(self):
        return pd.DataFrame([
            {"TableName": "Good", "ColumnName": "A", "PandasDataType": "Int64"},
            {"TableName": "Poison", "ColumnName": "B", "PandasDataType": "Int64"},
            {"TableName": "Tail", "ColumnName": "C", "PandasDataType": "Int64"},
        ])

    def get_table(self, name):
        if name == "Poison" and os.environ.get("SHIM_POISON") == "kill":
            os.kill(os.getpid(), signal.SIGKILL)
        if name == "Poison":
            raise ValueError("decode error")
        return pd.DataFrame({"x": [1, 2]})
'''


class TestIsolatedMaterialize:
    """Exercises the real worker subprocess via a PYTHONPATH pbixray shim."""

    @pytest.fixture()
    def shim(self, tmp_path, monkeypatch):
        shim_dir = tmp_path / "shim"
        shim_dir.mkdir()
        (shim_dir / "pbixray.py").write_text(_SHIM_PBIXRAY)
        existing = os.environ.get("PYTHONPATH", "")
        monkeypatch.setenv("PYTHONPATH", f"{shim_dir}{os.pathsep}{existing}" if existing else str(shim_dir))
        return shim_dir

    def test_clean_error_skipped_in_worker(self, shim, tmp_path, monkeypatch):
        monkeypatch.setenv("SHIM_POISON", "raise")
        manifest = materialize_pbix_parquets_isolated(
            str(tmp_path / "x.pbix"), tmp_path / "cache", timeout_s=120,
        )
        assert set(manifest) == {"Good", "Tail"}
        assert (tmp_path / "cache" / "manifest.json").exists()
        assert not (tmp_path / "cache" / "manifest.partial.json").exists()

    def test_worker_kill_excludes_table_and_retries(self, shim, tmp_path, monkeypatch):
        monkeypatch.setenv("SHIM_POISON", "kill")
        manifest = materialize_pbix_parquets_isolated(
            str(tmp_path / "x.pbix"), tmp_path / "cache", timeout_s=120,
        )
        # Worker was SIGKILLed on 'Poison'; parent excluded it and re-ran, and
        # the surviving tables (incl. one decoded before the kill) are served.
        assert set(manifest) == {"Good", "Tail"}
        for fname in manifest.values():
            assert (tmp_path / "cache" / fname).exists()


class TestWarmAll:
    def test_warm_and_failure_accounting(self, fake_pbixray, tmp_path, cache_dir):
        good = tmp_path / "good.pbix"
        _write_pbix(good)
        bad = tmp_path / "bad.pbix"
        bad.write_bytes(b"PK\x03\x04 truncated")
        c = PBIXClient(f"{tmp_path}/*.pbix")

        def _ensure(filepath):
            if "bad" in filepath:
                raise RuntimeError("boom")
            return c._cache_key(filepath)[1], {"Sales": "sales.parquet"}

        with patch.object(PBIXClient, "_ensure_parquets", side_effect=_ensure):
            paths = asyncio.run(c.awarm_all())
        assert len(paths) == 1

    def test_index_stats(self, tmp_path):
        _write_pbix(tmp_path / "a.pbix")
        stats = PBIXClient(f"{tmp_path}/*.pbix").index_stats()
        assert stats["file_count"] == 1
        assert stats["source_bytes"] > 0
