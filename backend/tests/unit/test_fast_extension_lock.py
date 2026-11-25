"""No statement loads extension code on a locked FAST serving engine (V1).

With the verified-once load (option V) a serving engine accepts unsigned
extension files, so the session lock has to keep every later load out. Each
row below is a statement that loads extension code on an engine with nothing
in the way. Every row runs on a fresh serving engine opened and built by the
product (`FastQueryClient._open_engine` with a trusted httpfs, then
`_build_session`, which ends with `lock_query_session`), once on the root
connection and once on a cursor. Six signed extensions sit installed but not
loaded in the engine's extension directory, beside httpfs. As in the image,
the postgres multi engine's own directory (BOW_DUCKDB_EXTENSION_DIR, postgres
only) exists beside it and its variable is set; the rows also try to load
from there, by name and by path.

The assertion for every row: no name is added to the engine's whole function,
setting and type catalogs. Each row's outcome is also compared with the
expected one (refused, and by which gate; `LOAD httpfs` again is accepted as a
no-op).

Controls, so that a DuckDB upgrade that turns a row into a non-trigger is
reported rather than silently passed:
- every trigger row, with nothing in the way (DuckDB's default config, the
  same directory, no lock), must add names;
- the explicit-load rows, on V's own engine without the lock, must add names.

Re-run on every DuckDB upgrade. Synthetic data only.
"""

import os
import shutil
import socket
from dataclasses import dataclass

import duckdb
import pytest

from app.data_sources.fast import artifacts, extension
from app.data_sources.fast import fast_client as fast_client_module
from app.data_sources.fast.fast_client import FastQueryClient, FastRelation
from tests.fixtures.fast_extension import (
    EXTRA_EXTENSIONS,
    PREPARED_ENV,
    extension_file,
    make_httpfs_dir,
    prepared_extensions_dir,  # noqa: F401 - fixture
)

# Gates, in the engine's own words.
AUTOLOAD_OFF = "auto-load off"          # "... is not in the catalog, but it exists in the X extension"
LOCKED_CONFIG = "lock_configuration"    # "the configuration has been locked"
FILE_SYSTEM = "file-system gate"        # "file system operations are disabled by configuration"
EXTENSION_LOAD = "extension-load gate"  # "Loading external extensions is disabled through configuration"
CATALOG = "catalog error"               # not a trigger in this DuckDB version
ACCEPTED = "accepted"                   # only `LOAD httpfs` again, a no-op, on the locked engine

MULTI_ENGINE_ENV = "BOW_DUCKDB_EXTENSION_DIR"


@dataclass(frozen=True)
class Row:
    name: str
    sql: str            # {ext}: a file in the engine's extension directory; {other}: postgres
                        # in the multi engine's directory; {garbage}; {work}
    locked: str         # the expected outcome on V's locked engine
    trigger: bool       # loads code with nothing in the way
    explicit: bool = False   # loads code on V's engine without the lock
    network: bool = False    # its control needs DuckDB's extension repository


ROWS = [
    Row("table function of an unloaded extension, no path", "SELECT * FROM tpch_queries()", AUTOLOAD_OFF, True),
    Row("CALL of an unloaded extension's function", "CALL dbgen(sf = 0)", AUTOLOAD_OFF, True),
    Row("scalar function of an unloaded extension", "SELECT excel_text(1234.5, '0.00')", AUTOLOAD_OFF, True),
    Row("PREPARE naming such a function", "PREPARE p AS SELECT * FROM tpch_queries()", AUTOLOAD_OFF, True),
    Row("EXPLAIN naming such a function", "EXPLAIN SELECT * FROM tpch_queries()", AUTOLOAD_OFF, True),
    Row("PRAGMA of an unloaded extension", "PRAGMA tpch(1)", AUTOLOAD_OFF, True),
    Row("type name of an unloaded extension, in a cast", "SELECT '127.0.0.1'::INET", AUTOLOAD_OFF, True),
    Row("type name of an unloaded extension, in DDL", "CREATE TEMP TABLE t_inet (a INET)", AUTOLOAD_OFF, True),
    Row("SET an extension-owned setting (sqlite)", "SET sqlite_all_varchar = true", LOCKED_CONFIG, True),
    Row("RESET an extension-owned setting (sqlite)", "RESET sqlite_all_varchar", LOCKED_CONFIG, True),
    Row("SET an extension-owned setting (postgres)", "SET pg_debug_show_queries = true", LOCKED_CONFIG, True),
    Row("SET an extension-owned setting (azure)", "SET azure_storage_connection_string = 'x'", LOCKED_CONFIG, True),
    Row("read an extension-owned setting", "SELECT current_setting('sqlite_all_varchar')", AUTOLOAD_OFF, True),
    Row("CREATE SECRET for an unloaded provider (azure)", "CREATE SECRET s_az (TYPE azure, CONNECTION_STRING 'x')", FILE_SYSTEM, True),
    Row("CREATE TEMPORARY SECRET, unloaded provider (azure)", "CREATE TEMPORARY SECRET s_azt (TYPE azure, CONNECTION_STRING 'x')", FILE_SYSTEM, True),
    Row("CREATE TEMPORARY SECRET, unloaded provider (postgres)", "CREATE TEMPORARY SECRET s_pg (TYPE postgres, HOST 'h')", FILE_SYSTEM, True),
    Row("CREATE TEMPORARY SECRET, loaded provider (s3)", "CREATE TEMPORARY SECRET s_s3 (TYPE s3, KEY_ID 'a', SECRET 'b')", FILE_SYSTEM, False),
    Row("ATTACH ':memory:' with TYPE sqlite", "ATTACH ':memory:' AS x_sqlite (TYPE sqlite)", EXTENSION_LOAD, True, explicit=True),
    Row("ATTACH with TYPE postgres", "ATTACH '' AS x_pg (TYPE postgres)", EXTENSION_LOAD, True, explicit=True),
    Row("ATTACH with TYPE mysql (not installed)", "ATTACH '' AS x_my (TYPE mysql)", EXTENSION_LOAD, True, network=True),
    Row("ATTACH with a storage prefix", "ATTACH 'sqlite:x.db' AS x_prefix", EXTENSION_LOAD, True, explicit=True),
    Row("COPY TO a file with an extension format", "COPY (SELECT 1 AS a) TO 'out.xlsx' (FORMAT xlsx)", CATALOG, False),
    Row("COPY TO stdout with an extension format", "COPY (SELECT 1 AS a) TO '/dev/stdout' (FORMAT xlsx)", CATALOG, False),
    Row("COPY FROM with an extension format", "COPY t_none FROM 'in.xlsx' (FORMAT xlsx)", CATALOG, False),
    Row("function of an unloaded extension with a path", "SELECT * FROM sqlite_scan('x.db', 't')", AUTOLOAD_OFF, True),
    Row("remote path that would auto-load", "SELECT * FROM read_parquet('az://c/f.parquet')", FILE_SYSTEM, True),
    Row("INSTALL a known extension", "INSTALL tpcds", FILE_SYSTEM, False),
    Row("INSTALL ... FROM community", "INSTALL h3 FROM community", FILE_SYSTEM, False),
    Row("INSTALL ... FROM a URL", "INSTALL tpcds FROM 'http://127.0.0.1:9/none'", FILE_SYSTEM, False),
    Row("FORCE INSTALL a local path", "FORCE INSTALL '{ext[tpch]}'", FILE_SYSTEM, False),
    Row("UPDATE EXTENSIONS", "UPDATE EXTENSIONS", FILE_SYSTEM, False),
    Row("LOAD by name", "LOAD tpch", EXTENSION_LOAD, True, explicit=True),
    Row("LOAD by an alias name (sqlite)", "LOAD sqlite", EXTENSION_LOAD, True, explicit=True),
    Row("LOAD by an alias name (postgres)", "LOAD postgres", EXTENSION_LOAD, True, explicit=True),
    Row("LOAD by file path, a real signed extension", "LOAD '{ext[tpch]}'", EXTENSION_LOAD, True, explicit=True),
    Row("LOAD by file path, postgres in the multi engine's directory", "LOAD '{other}'", EXTENSION_LOAD, True, explicit=True),
    Row("function of postgres, the multi engine's extension", "SELECT * FROM postgres_query('', 'SELECT 1')", AUTOLOAD_OFF, True),
    Row("LOAD by file path, a file that is not an extension", "LOAD '{garbage}'", EXTENSION_LOAD, False),
    Row("LOAD httpfs again (already loaded)", "LOAD httpfs", ACCEPTED, False),
    Row("multi-statement: SELECT 1; LOAD tpch", "SELECT 1; LOAD tpch", EXTENSION_LOAD, True, explicit=True),
    Row("multi-statement: LOAD tpch; SELECT from its function", "LOAD tpch; SELECT * FROM tpch_queries()", EXTENSION_LOAD, True, explicit=True),
    Row("multi-statement: INSTALL tpch; LOAD tpch", "INSTALL tpch; LOAD tpch", FILE_SYSTEM, True, explicit=True),
    Row("SET allow_unsigned_extensions", "SET allow_unsigned_extensions = true", LOCKED_CONFIG, False),
    Row("SET autoload_known_extensions", "SET autoload_known_extensions = true", LOCKED_CONFIG, False),
    Row("SET autoinstall_known_extensions", "SET autoinstall_known_extensions = true", LOCKED_CONFIG, False),
    Row("SET allow_community_extensions", "SET allow_community_extensions = true", LOCKED_CONFIG, False),
    Row("SET extension_directory", "SET extension_directory = '{work}'", LOCKED_CONFIG, False),
    Row("SET custom_extension_repository", "SET custom_extension_repository = 'http://127.0.0.1:9/none'", LOCKED_CONFIG, False),
    Row("list extensions", "SELECT extension_name FROM duckdb_extensions() WHERE loaded", FILE_SYSTEM, False),
]
NON_TRIGGERS = [r.name for r in ROWS if not r.trigger]

CATALOG_NAMES = (
    "SELECT list(m) FROM ("
    "SELECT DISTINCT 'fn:' || function_name AS m FROM duckdb_functions() "
    "UNION SELECT 'set:' || name FROM duckdb_settings() "
    "UNION SELECT DISTINCT 'type:' || lower(type_name) FROM duckdb_types())"
)


def test_the_row_table_has_every_row_once():
    names = [r.name for r in ROWS]
    assert len(names) == len(set(names)) == 49
    assert sum(r.trigger for r in ROWS) == 31
    # Rows that are not triggers in this DuckDB version are listed, not hidden.
    assert len(NON_TRIGGERS) == 18


def gate_of(exc: Exception) -> str:
    t = str(exc)
    if "Loading external extensions is disabled" in t:
        return EXTENSION_LOAD
    if "configuration has been locked" in t:
        return LOCKED_CONFIG
    if "file system operations are disabled" in t:
        return FILE_SYSTEM
    if "is not in the catalog, but it exists in the" in t:
        return AUTOLOAD_OFF
    if isinstance(exc, duckdb.CatalogException):
        return CATALOG
    return "other: " + type(exc).__name__ + ": " + t.splitlines()[0][:160]


def catalog(con) -> set:
    return set(con.execute(CATALOG_NAMES).fetchone()[0] or [])


def run_row(con, sql: str, who: str):
    """Run `sql` on the root or a cursor; return (outcome, names added)."""
    before = catalog(con)
    runner = con if who == "root" else con.cursor()
    try:
        runner.execute(sql)
        try:
            runner.fetchall()
        except Exception:  # noqa: BLE001 - statements without a result
            pass
        outcome = ACCEPTED
    except Exception as e:  # noqa: BLE001 - the refusal is the result
        outcome = gate_of(e)
    return outcome, sorted(catalog(con) - before)


@dataclass
class Lab:
    work: str
    extensions: str     # the engine's extension directory
    httpfs: str
    extract: str
    key: str
    garbage: str
    other: str          # the multi engine's directory: postgres only

    def sql(self, row: Row, ext_dir: str | None = None) -> str:
        d = ext_dir or self.extensions
        ext = {n: extension_file(d, n).replace("'", "''") for n in EXTRA_EXTENSIONS}
        other = extension_file(self.other, "postgres_scanner").replace("'", "''")
        return row.sql.format(ext=ext, other=other, garbage=self.garbage, work=self.work)


@pytest.fixture(scope="module")
def lab(prepared_extensions_dir, tmp_path_factory):
    work = tmp_path_factory.mktemp("v1")
    directory = work / "duckdb_httpfs"
    httpfs = make_httpfs_dir(prepared_extensions_dir, directory)
    # Verified as V verifies it, while it holds httpfs alone ...
    assert extension.trusted_httpfs(str(directory), require_foreign_owner=False) == httpfs
    # ... then six more signed extensions appear beside it, installed and not
    # loaded: a stronger setting than production, where the directory holds
    # httpfs alone.
    for name in EXTRA_EXTENSIONS:
        src = extension_file(prepared_extensions_dir, name)
        shutil.copy2(src, extension_file(directory, name))
        if os.path.isfile(src + ".info"):
            shutil.copy2(src + ".info", extension_file(directory, name) + ".info")
    # The postgres multi engine's directory, as in the image: postgres only,
    # its variable set. FAST never reads it.
    other = work / "duckdb_extensions"
    postgres = extension_file(other, "postgres_scanner")
    os.makedirs(os.path.dirname(postgres))
    shutil.copy2(extension_file(prepared_extensions_dir, "postgres_scanner"), postgres)
    garbage = work / "garbage.duckdb_extension"
    garbage.write_bytes(b"not an extension " * 256)
    key = artifacts.new_artifact_key()
    extract = str(work / "txns.duckdb")
    w = artifacts.connect_encrypted(extract, key)
    w.execute("CREATE TABLE txns AS SELECT i AS txn_id, 'Branch ' || (i % 40) AS branch FROM range(1000) t(i)")
    w.close()
    old = os.getcwd()
    env = pytest.MonkeyPatch()
    env.setenv(MULTI_ENGINE_ENV, str(other))
    os.chdir(work)  # relative paths in the rows land in the lab
    try:
        yield Lab(str(work), str(directory), httpfs, extract, key, str(garbage), str(other))
    finally:
        os.chdir(old)
        env.undo()


def serving_engine(lab: Lab):
    """A FAST serving engine as the product opens and builds it under V."""
    client = FastQueryClient([])
    con = client._open_engine(trusted_extension=lab.httpfs)
    try:
        client._build_session(con, [FastRelation("txns", lab.extract, lab.key, columns=[])])
    except Exception:
        con.close()
        raise
    return con


def test_the_serving_engine_has_vs_config_and_is_locked(lab):
    con = serving_engine(lab)
    try:
        got = con.execute(
            "SELECT current_setting('allow_unsigned_extensions'), "
            "current_setting('autoload_known_extensions'), "
            "current_setting('autoinstall_known_extensions'), "
            "current_setting('allow_community_extensions'), "
            "current_setting('extension_directory'), "
            "current_setting('lock_configuration'), "
            "current_setting('enable_external_access')"
        ).fetchone()
        assert got == (True, False, False, False, lab.extensions, True, False)
        assert con.execute('SELECT count(*) FROM "txns"').fetchone()[0] == 1000
        assert "fn:tpch_queries" not in catalog(con)
    finally:
        con.close()


def signed_serving_engine(lab: Lab, monkeypatch):
    """The other serving shape: V off, httpfs loaded by name from the
    httpfs directory (BOW_FAST_EXTENSION_DIR) with signature checks and
    DuckDB's default auto-load and auto-install, the six extra extensions
    beside it, then the same session build and lock."""
    monkeypatch.delenv(extension.SWITCH_ENV, raising=False)
    monkeypatch.setenv(extension.DIRECTORY_ENV, lab.extensions)
    client = FastQueryClient([])
    con = client._open_engine()
    try:
        client._build_session(con, [FastRelation("txns", lab.extract, lab.key, columns=[])])
    except Exception:
        con.close()
        raise
    return con


def test_the_signed_serving_engine_loads_httpfs_from_the_directory_and_is_locked(lab, monkeypatch):
    con = signed_serving_engine(lab, monkeypatch)
    try:
        got = con.execute(
            "SELECT current_setting('allow_unsigned_extensions'), "
            "current_setting('extension_directory'), "
            "current_setting('lock_configuration'), "
            "current_setting('enable_external_access')"
        ).fetchone()
        assert got == (False, lab.extensions, True, False)
        assert "fn:tpch_queries" not in catalog(con)
        assert con.execute('SELECT count(*) FROM "txns"').fetchone()[0] == 1000
    finally:
        con.close()


@pytest.mark.parametrize("shape", ["v", "signed"])
@pytest.mark.parametrize("who", ["root", "cursor"])
@pytest.mark.parametrize("row", ROWS, ids=[r.name for r in ROWS])
def test_no_statement_loads_code_on_a_locked_serving_engine(
    lab, row, who, shape, monkeypatch, record_property
):
    con = serving_engine(lab) if shape == "v" else signed_serving_engine(lab, monkeypatch)
    try:
        outcome, added = run_row(con, lab.sql(row), who)
    finally:
        con.close()
    record_property("outcome", outcome)
    record_property("names_added", len(added))
    assert added == [], f"{row.name} ({who}, {shape}) loaded code: {added[:5]}"
    if shape == "v":
        assert outcome == row.locked
    else:
        # Auto-load is on here, so the gates differ from V's; every row but
        # `LOAD httpfs` again is still refused.
        assert (outcome == ACCEPTED) == (row.locked == ACCEPTED), outcome


def _network_reachable() -> bool:
    try:
        with socket.create_connection(("extensions.duckdb.org", 443), timeout=3):
            return True
    except OSError:
        return False


@pytest.fixture
def row_dir(prepared_extensions_dir, tmp_path):
    """A private copy of the extension directory for one control row, so no row
    sees what another installed."""
    d = tmp_path / "ext"
    shutil.copytree(prepared_extensions_dir, d, copy_function=os.link)
    return str(d)


@pytest.mark.parametrize("who", ["root", "cursor"])
@pytest.mark.parametrize(
    "row", [r for r in ROWS if r.trigger], ids=[r.name for r in ROWS if r.trigger]
)
def test_every_trigger_row_loads_code_with_nothing_in_the_way(lab, row, who, row_dir, record_property):
    if row.network and not _network_reachable():
        reason = f"needs the network: '{row.name}' fetches an extension from DuckDB's repository"
        if os.environ.get(PREPARED_ENV):
            pytest.fail(reason + f" ({PREPARED_ENV} is set, so this run must have it)")
        pytest.skip(reason)
    con = duckdb.connect(":memory:", config={"extension_directory": row_dir})
    try:
        con.execute(f"ATTACH '{lab.extract}' AS bow_a0 (ENCRYPTION_KEY '{lab.key}', READ_ONLY)")
        con.execute('CREATE VIEW "txns" AS SELECT * FROM bow_a0."txns"')
        outcome, added = run_row(con, lab.sql(row, row_dir), who)
    finally:
        con.close()
    record_property("control_outcome", outcome)
    record_property("control_names_added", len(added))
    assert added, f"{row.name} ({who}) is no longer a trigger: {outcome}"


@pytest.mark.parametrize("who", ["root", "cursor"])
@pytest.mark.parametrize(
    "row", [r for r in ROWS if r.explicit], ids=[r.name for r in ROWS if r.explicit]
)
def test_without_the_lock_explicit_loads_load_code_on_vs_engine(lab, row, who, monkeypatch, record_property):
    monkeypatch.setattr(fast_client_module, "lock_query_session", lambda con: None)
    con = serving_engine(lab)
    try:
        assert con.execute("SELECT current_setting('lock_configuration')").fetchone()[0] is False
        outcome, added = run_row(con, lab.sql(row), who)
    finally:
        con.close()
    record_property("unlocked_outcome", outcome)
    record_property("unlocked_names_added", len(added))
    assert added, f"{row.name} ({who}) loaded nothing on V's engine without the lock: {outcome}"
