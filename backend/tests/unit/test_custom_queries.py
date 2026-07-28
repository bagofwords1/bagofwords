"""Custom queries: the guarantees that must not regress.

A custom query is admin-authored SQL materialized to an encrypted local DuckDB
artifact and served to agents from there instead of the source. Four properties
carry the whole feature, and each has a silent failure mode:

1. **A reindex must not delete custom queries.** They live in
   ``connection_tables`` alongside introspected rows but have no counterpart in
   the source catalog, so an unfiltered stale-row sweep would remove every one
   of them on the next scheduled reindex — silently, and with the artifacts.

2. **Artifacts must be unreadable without the key.** Encryption is the sandbox
   boundary, not just at-rest compliance: generated Python has ``pd`` injected
   and pandas does its own IO, so a plain artifact could be read straight off
   disk, bypassing the catalog entirely.

3. **A huge query must be refused before it runs.** `SELECT *` over a
   billion-row table must not be absorbed.

4. **The serving session must stay locked down.** With pandas denied access,
   DuckDB SQL becomes the remaining surface.
"""

import base64
import secrets

import duckdb
import pytest

from app.ai.code_execution.code_execution import UnsafePythonError, validate_python_code
from app.data_sources.fast import artifacts, extractor
from app.data_sources.fast.fast_client import FastQueryClient, FastRelation
from app.models.connection_table import KIND_BOW, KIND_TABLE


# --------------------------------------------------------------------------
# 1. Reindex must not touch kind='bow' rows
# --------------------------------------------------------------------------

def test_refresh_schema_only_considers_introspected_rows():
    """``refresh_schema`` must scope its catalog query to ``kind='table'``.

    Asserted against the source because the failure is invisible at runtime
    until a scheduled reindex fires and the custom queries are already gone.
    """
    import inspect

    from app.services import connection_service

    src = inspect.getsource(connection_service.ConnectionService.refresh_schema)
    assert "ConnectionTable.kind == KIND_TABLE" in src, (
        "refresh_schema must filter existing_tables to kind='table'; without it "
        "every kind='bow' custom query lands in the `missing` set and is deleted."
    )


def test_kind_constants_are_distinct():
    assert KIND_TABLE == "table"
    # Deliberately not "view": a view implies non-materialized.
    assert KIND_BOW == "bow"
    assert KIND_TABLE != KIND_BOW


# --------------------------------------------------------------------------
# 2. Artifact encryption
# --------------------------------------------------------------------------

def test_artifact_is_encrypted_and_needs_its_key(tmp_path):
    path = tmp_path / "a.db"
    key = artifacts.new_artifact_key()

    con = artifacts.connect_encrypted(path, key)
    con.execute("CREATE TABLE t AS SELECT 'sensitive-value' AS v")
    con.close()

    # The plaintext must not be recoverable from the raw bytes.
    assert b"sensitive-value" not in path.read_bytes()

    # Correct key reads it back.
    con = artifacts.connect_encrypted(path, key, read_only=True)
    assert con.execute("SELECT v FROM t").fetchall() == [("sensitive-value",)]
    con.close()

    # A different key must not.
    other = artifacts.new_artifact_key()
    with pytest.raises(Exception):
        artifacts.connect_encrypted(path, other, read_only=True)


def test_pandas_cannot_read_an_artifact(tmp_path):
    """The sandbox boundary: pandas must not be able to open the file."""
    import pandas as pd

    path = tmp_path / "b.db"
    key = artifacts.new_artifact_key()
    con = artifacts.connect_encrypted(path, key)
    con.execute("CREATE TABLE t AS SELECT 1 AS x")
    con.close()

    with pytest.raises(Exception):
        pd.read_parquet(str(path))


def test_artifact_key_roundtrips_through_fernet():
    key = artifacts.new_artifact_key()
    enc = artifacts.encrypt_key(key)
    assert enc != key
    assert artifacts.decrypt_key(enc) == key


def test_artifact_paths_are_opaque(tmp_path, monkeypatch):
    """Filenames must not be derivable from the relation or connection name."""
    monkeypatch.setattr(artifacts, "_ARTIFACT_ROOT", tmp_path)
    p1 = artifacts.new_artifact_path("conn-1")
    p2 = artifacts.new_artifact_path("conn-1")
    assert p1 != p2
    assert "conn-1" not in p1.name
    assert p1.suffix == ".db"


# --------------------------------------------------------------------------
# 3. Extraction budgets
# --------------------------------------------------------------------------

def test_budget_refuses_on_row_count():
    est = extractor.Estimate(rows=2_000_000_000, width_bytes=40,
                             total_bytes=80_000_000_000, supported=True)
    with pytest.raises(extractor.ExtractionRefused) as e:
        extractor.check_budget(est)
    assert "2,000,000,000 rows" in str(e.value)


def test_budget_refuses_on_byte_size():
    est = extractor.Estimate(rows=10, width_bytes=10**9,
                             total_bytes=10 * 10**9, supported=True)
    with pytest.raises(extractor.ExtractionRefused):
        extractor.check_budget(est)


def test_budget_allows_a_reasonable_query():
    est = extractor.Estimate(rows=50_000, width_bytes=80,
                             total_bytes=4_000_000, supported=True)
    extractor.check_budget(est)  # must not raise


def test_budget_is_permissive_when_no_estimate_available():
    """An engine without EXPLAIN support falls through to the hard caps rather
    than blocking every query."""
    extractor.check_budget(extractor.Estimate(supported=False))


def test_preview_limit_is_one_hundred():
    assert extractor.PREVIEW_ROW_LIMIT == 100


# --------------------------------------------------------------------------
# 4. Serving session lockdown + authorization
# --------------------------------------------------------------------------

def _relation(tmp_path, name="rel", value=1):
    path = tmp_path / f"{name}.db"
    key = artifacts.new_artifact_key()
    con = artifacts.connect_encrypted(path, key)
    con.execute(f'CREATE TABLE "{name}" AS SELECT {value} AS x')
    con.close()
    return FastRelation(name, str(path), key, [{"name": "x", "dtype": "int64"}],
                        as_of="2026-07-28T00:00", row_count=1)


def test_fast_client_serves_its_relation(tmp_path):
    rel = _relation(tmp_path, "orders_summary", 42)
    df = FastQueryClient([rel]).execute_query("SELECT x FROM orders_summary")
    assert df["x"].tolist() == [42]


def test_unactivated_relation_is_not_nameable(tmp_path):
    """Authorization is structural: a relation absent from the catalog cannot
    be referenced, regardless of what the generated SQL asks for."""
    granted = _relation(tmp_path, "granted", 1)
    _withheld = _relation(tmp_path, "withheld", 2)

    client = FastQueryClient([granted])
    with pytest.raises(Exception):
        client.execute_query("SELECT * FROM withheld")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_parquet('/etc/hostname')",
    "SELECT * FROM read_csv_auto('/etc/passwd')",
    "COPY (SELECT 1) TO '/tmp/bow_leak_test.csv'",
    "SET enable_external_access=true",
])
def test_serving_session_is_locked_down(tmp_path, sql):
    """With pandas locked out, DuckDB SQL is the remaining surface: no reading
    arbitrary files, no attaching other artifacts, no writing anything out."""
    client = FastQueryClient([_relation(tmp_path, "rel", 1)])
    with pytest.raises(Exception):
        client.execute_query(sql)


def test_attaching_another_artifact_is_blocked(tmp_path):
    mine = _relation(tmp_path, "mine", 1)
    theirs = _relation(tmp_path, "theirs", 2)
    client = FastQueryClient([mine])
    with pytest.raises(Exception):
        client.execute_query(
            f"ATTACH '{theirs.artifact_path}' AS o "
            f"(ENCRYPTION_KEY '{theirs.artifact_key}')"
        )


def test_description_carries_dialect_freshness_and_scan_hint(tmp_path):
    """These three cues drive codegen quality: without the dialect the model
    writes source SQL, without 'as of' it reports stale figures as current, and
    without the cheap-scan cue it keeps pre-aggregating defensively."""
    d = FastQueryClient([_relation(tmp_path, "rel", 1)]).description
    assert "DuckDB SQL" in d
    assert "Scans are cheap" in d
    assert "data as of" in d
    assert "rel" in d


# --------------------------------------------------------------------------
# 5. Sandbox validator: hardcoded paths blocked, uploaded files still allowed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code", [
    "def generate_df(ds, ex):\n    return pd.read_parquet('/app/uploads/fast/x.db')",
    "def generate_df(ds, ex):\n    return pd.read_csv('/etc/passwd')",
    "def generate_df(ds, ex):\n    return pd.read_parquet(f'/app/uploads/{1}.db')",
    "def generate_df(ds, ex):\n    return np.load('/app/uploads/x.npy')",
])
def test_hardcoded_file_reads_are_rejected(code):
    with pytest.raises(UnsafePythonError):
        validate_python_code(code)


@pytest.mark.parametrize("code", [
    # The sanctioned way to read an uploaded file — must keep working.
    "def generate_df(ds, ex):\n    return pd.read_excel(ex[0].path, sheet_name=0, header=None)",
    "def generate_df(ds, ex):\n    return pd.read_csv(ex[0].path)",
    "def generate_df(ds, ex):\n    return pd.read_json(ex[1].path, lines=True)",
    "def generate_df(ds, ex):\n    p = ex[0].path\n    return pd.read_csv(p)",
    "def generate_df(ds, ex):\n    return ds['a:b'].execute_query('SELECT 1')",
])
def test_uploaded_file_reads_still_allowed(code):
    validate_python_code(code)  # must not raise
