"""File-based connectors stay inside the data they were configured with.

DuckDB (duckdb / csv) and SQLite run SQL inside the API process. A connection
must keep answering queries over its own tables, and must not be a way to read
or write any other file, attach another database, or load code.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from app.ai.code_execution.duckdb_guard import ConnectionTableViolation, DuckdbQueryError
from app.ai.code_execution.sql_guard import ReadOnlyViolation, check_sql, ensure_read_only
from app.ai.code_execution.table_scope import TableScopeViolation
from app.data_sources.clients import _duckdb_lockdown as lockdown
from app.data_sources.clients.csv_client import CSVClient
from app.data_sources.clients.duckdb_client import DuckDBClient
from app.data_sources.clients.pbix_client import PBIXClient
from app.data_sources.clients.qvd_client import QVDClient
from app.data_sources.clients.sqlite_client import SqliteClient
from tests.unit.test_duckdb_client_guard import http_base  # noqa: F401  (a local HTTP server)
from tests.unit.test_lakehouse_clients import iceberg_catalog  # noqa: F401  (a real Iceberg catalog)

_BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture
def data(tmp_path):
    """A normal data folder, plus a file next to it that no connection names."""
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "sales.csv").write_text("region,amount\nnorth,10\nsouth,32\n")
    (folder / "returns.csv").write_text("region,amount\nnorth,1\n")
    pd.DataFrame({"sku": ["a", "b", "c"], "qty": [1, 2, 3]}).to_parquet(folder / "stock.parquet")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "private.txt").write_text("not part of any connection")
    (outside / "private.csv").write_text("k,v\nsecret,1\n")
    return folder


@pytest.fixture
def outside(data):
    return data.parent / "elsewhere"


@pytest.fixture
def unrestricted(monkeypatch):
    from app.settings.config import settings

    monkeypatch.setattr(settings.bow_config.security, "unsafe_allow_unrestricted_file_connectors", True)


def _duckdb_file(path: Path) -> Path:
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE prices AS SELECT 'acme' AS ticker, 12.5 AS price")
    con.close()
    return path


def _folder(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _refused_where_checked(client, sql: str) -> None:
    """The read-only guard refuses `sql` for `client` before it runs. A
    DuckDB connection is checked in the code-execution wrapper
    (`ensure_read_only`). The file connections (CSV, QVD, PBIX) are exempt
    there and checked at their own dispatch point instead, on their locked
    session (`client_profiles.EXEMPT_CLIENTS`); their refusal says whether a
    statement or a table was refused, or that a DESCRIBE / SHOW is not
    available there."""
    if isinstance(client, (CSVClient, QVDClient, PBIXClient)):
        assert ensure_read_only(client, sql) == sql
        with pytest.raises((ReadOnlyViolation, TableScopeViolation, DuckdbQueryError)):
            client.execute_query(sql)
    else:
        with pytest.raises(ReadOnlyViolation):
            ensure_read_only(client, sql)


def _sqlite_file(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, total REAL)")
    con.executemany("INSERT INTO orders (total) VALUES (?)", [(5.0,), (7.5,)])
    con.commit()
    con.close()
    return path


def _clients(data: Path, tmp_path: Path) -> dict:
    """One client per way a DuckDB-backed connection builds its session."""
    return {
        "csv_files": CSVClient(file_paths=str(data / "*.csv")),
        "duckdb_glob": DuckDBClient(uris=str(data / "*.csv")),
        "duckdb_exact_files": DuckDBClient(uris=f"{data / 'sales.csv'}\n{data / 'stock.parquet'}"),
        "duckdb_database": DuckDBClient(database=str(_duckdb_file(_folder(tmp_path / "warehouse") / "market.duckdb"))),
    }


_OWN_QUERY = {
    "csv_files": ("SELECT SUM(amount) AS total FROM sales", 42),
    "duckdb_glob": ("SELECT SUM(amount) AS total FROM data", 43),
    "duckdb_exact_files": ("SELECT SUM(qty) AS total FROM stock", 6),
    "duckdb_database": ("SELECT COUNT(*) AS total FROM prices", 1),
}
_KINDS = sorted(_OWN_QUERY)


# -- a connection keeps serving its own data ---------------------------------


@pytest.mark.parametrize("kind", _KINDS)
def test_connection_queries_its_own_tables(kind, data, tmp_path):
    client = _clients(data, tmp_path)[kind]
    sql, expected = _OWN_QUERY[kind]
    ensure_read_only(client, sql)
    assert client.execute_query(sql)["total"][0] == expected
    assert client.test_connection()["success"] is True
    assert client.get_tables(), "the catalog is still discoverable"


def test_csv_join_and_duckdb_syntax_still_work(data):
    client = CSVClient(file_paths=str(data / "*.csv"))
    df = client.execute_query(
        "SELECT s.region, s.amount - COALESCE(r.amount, 0) AS net "
        "FROM sales s LEFT JOIN returns r USING (region) "
        "QUALIFY row_number() OVER (ORDER BY net DESC) = 1"
    )
    assert df.to_dict("records") == [{"region": "south", "net": 32}]
    # A file connection answers DESCRIBE and SUMMARIZE with an error the
    # agent fixes by rewriting the query (SELECT ... LIMIT 0, aggregates).
    for sql in ("DESCRIBE sales", "SUMMARIZE sales"):
        with pytest.raises(DuckdbQueryError, match="LIMIT 0"):
            client.execute_query(sql)


def test_qvd_connection_queries_its_converted_table_and_nothing_else(data, outside, tmp_path, monkeypatch):
    """A QVD is served from its converted Parquet copy; the session is held to that copy."""
    from app.data_sources.clients import qvd_client

    monkeypatch.setattr(qvd_client, "_CACHE_DIR", tmp_path / "qvd_cache")
    source = data / "Sales.qvd"
    source.write_bytes(b"placeholder: the conversion below stands in for the converter binary")
    client = QVDClient(file_paths=str(data / "*.qvd"))
    _, converted = client._cache_key(str(source))
    converted.parent.mkdir(parents=True)
    pd.DataFrame({"region": ["north", "south"], "amount": [10, 32]}).to_parquet(converted)

    assert client.execute_query("SELECT SUM(amount) AS total FROM sales")["total"][0] == 42
    with client.connect() as con:
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT * FROM read_text('{outside / 'private.txt'}')").fetchall()
        with pytest.raises(duckdb.Error):
            con.execute(f"COPY (SELECT 1 AS a) TO '{converted.parent / 'new.parquet'}'")
    with pytest.raises(Exception):
        client.execute_query(f"SELECT * FROM read_parquet('{data / 'stock.parquet'}')")


def test_sqlite_connection_reads_its_tables(tmp_path):
    client = SqliteClient(database=str(_sqlite_file(tmp_path / "shop.sqlite")))
    assert client.execute_query("SELECT SUM(total) AS total FROM orders")["total"][0] == 12.5
    assert [t.name for t in client.get_tables()] == ["orders"]
    assert [c.name for c in client.get_tables()[0].columns] == ["id", "total"]
    assert client.test_connection()["success"] is True
    assert len(client.execute_query("PRAGMA table_info(orders)")) == 2


# -- nothing else on the server is readable ----------------------------------


def _reads_of(path: Path) -> list[str]:
    return [
        f"SELECT * FROM read_text('{path}')",
        f"SELECT * FROM read_blob('{path}')",
        f"SELECT * FROM read_csv_auto('{path}')",
        f"SELECT * FROM read_csv('{path}', header = false)",
        f"SELECT * FROM '{path}'",
        f"SELECT * FROM glob('{path.parent}/*')",
        f"WITH x AS (SELECT * FROM read_text('{path}')) SELECT * FROM x",
        f"SELECT (SELECT content FROM read_text('{path}'))",
        f"SELECT * FROM read_text('{path.parent}/' || '{path.name}')",
    ]


@pytest.mark.parametrize("kind", _KINDS)
def test_query_cannot_read_a_file_outside_the_connection(kind, data, outside, tmp_path):
    client = _clients(data, tmp_path)[kind]
    for sql in _reads_of(outside / "private.csv"):
        _refused_where_checked(client, sql)
        with pytest.raises(Exception):
            client.execute_query(sql)


@pytest.mark.parametrize("kind", _KINDS)
def test_session_itself_refuses_files_outside_the_connection(kind, data, outside, tmp_path):
    """The engine holds the line without the SQL guard in front of it."""
    client = _clients(data, tmp_path)[kind]
    with client.connect() as con:
        for sql in _reads_of(outside / "private.csv") + _reads_of(Path("/etc/hostname")):
            with pytest.raises(duckdb.Error):
                con.execute(sql).fetchall()
        # Relative traversal out of an allowed folder is not a way around it.
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT * FROM read_text('{data}/../elsewhere/private.txt')").fetchall()


def test_exact_file_connection_cannot_read_a_sibling_file(data):
    client = DuckDBClient(uris=str(data / "sales.csv"))
    with client.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 2
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT * FROM read_csv_auto('{data / 'returns.csv'}')").fetchall()


def test_folder_connection_cannot_read_a_folder_with_the_same_prefix(data):
    lookalike = data.parent / "data_private"
    lookalike.mkdir()
    (lookalike / "x.csv").write_text("a\n1\n")
    with DuckDBClient(uris=str(data / "*.csv")).connect() as con:
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT * FROM read_csv_auto('{lookalike / 'x.csv'}')").fetchall()


# -- nothing is writable, attachable or loadable -----------------------------


def _side_effects(target: Path) -> list[str]:
    return [
        f"COPY (SELECT 1 AS a) TO '{target / 'out.csv'}'",
        f"COPY (SELECT 'x') TO '{target / 'out.py'}' (FORMAT csv, HEADER false)",
        f"EXPORT DATABASE '{target / 'dump'}'",
        f"SELECT 1; EXPORT DATABASE '{target / 'dump'}'",
        f"ATTACH '{target / 'other.db'}' AS other",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SET enable_external_access = true",
        "RESET lock_configuration",
        "CREATE TABLE t AS SELECT 1 AS a",
        f"IMPORT DATABASE '{target}'",
        "CHECKPOINT",
    ]


@pytest.mark.parametrize("kind", _KINDS)
def test_guard_refuses_statements_with_side_effects(kind, data, outside, tmp_path):
    client = _clients(data, tmp_path)[kind]
    for sql in _side_effects(outside):
        _refused_where_checked(client, sql)
        with pytest.raises(Exception):
            client.execute_query(sql)
    assert sorted(p.name for p in outside.iterdir()) == ["private.csv", "private.txt"]


@pytest.mark.parametrize("client_cls", [CSVClient, QVDClient, PBIXClient])
def test_guard_covers_every_in_process_file_connector(client_cls, tmp_path):
    client = client_cls(file_paths=str(tmp_path / "*"))
    ensure_read_only(client, "SELECT * FROM sales LIMIT 5")
    for sql in ("COPY sales TO '/tmp/x.csv'", "ATTACH '/tmp/x.db' AS x", "INSTALL httpfs",
                "EXPORT DATABASE '/tmp/x'", "SELECT * FROM read_text('/etc/hostname')"):
        with pytest.raises(ReadOnlyViolation):
            client.execute_query(sql)


@pytest.mark.parametrize("kind", _KINDS)
def test_session_itself_refuses_writes_outside_and_reconfiguration(kind, data, outside, tmp_path):
    client = _clients(data, tmp_path)[kind]
    statements = [
        f"COPY (SELECT 1 AS a) TO '{outside / 'out.csv'}'",
        f"EXPORT DATABASE '{outside / 'dump'}'",
        f"ATTACH '{outside / 'other.db'}' AS other",
        "INSTALL spatial",
        "LOAD spatial",
        "SET enable_external_access = true",
        "RESET lock_configuration",
    ]
    with client.connect() as con:
        for sql in statements:
            with pytest.raises(duckdb.Error):
                con.execute(sql)
    assert sorted(p.name for p in outside.iterdir()) == ["private.csv", "private.txt"]


def test_csv_session_cannot_overwrite_or_add_files_in_its_own_folder(data):
    before = sorted(p.name for p in data.iterdir())
    with CSVClient(file_paths=str(data / "*.csv")).connect() as con:
        with pytest.raises(duckdb.Error):
            con.execute(f"COPY (SELECT 1 AS a) TO '{data / 'new.csv'}'")
    assert sorted(p.name for p in data.iterdir()) == before


def test_unparseable_text_is_not_waved_through():
    """Text the SQL parser cannot read is decided by the engine's own parser:
    one SELECT, or refused."""
    for sql in ("EXPORT DATABASE '/tmp/zz'", "SELECT 1; EXPORT DATABASE '/tmp/zz'",
                "IMPORT DATABASE '/tmp/zz'", "this is not sql"):
        with pytest.raises(ReadOnlyViolation):
            check_sql(sql, "duckdb")


def test_other_dialects_refuse_a_second_statement_in_unparsed_text():
    with pytest.raises(ReadOnlyViolation):
        check_sql("SELECT 1 FROM t WHERE a ~~~ 'x'; FROBNICATE everything", "postgres")


# -- path policy --------------------------------------------------------------


_PROTECTED = [
    str(_BACKEND / "app" / "main.py"),
    str(_BACKEND / "uploads" / "files" / "report.csv"),
    str(_BACKEND / "uploads" / "git_state" / "known_hosts"),
    str(_BACKEND / "uploads" / "filecache" / "x.csv"),
    str(_BACKEND / "uploads" / "demo_data" / "some-other-org" / "x.sqlite"),
    str(_BACKEND / "configs" / "bow-config.yaml"),
    str(_BACKEND / ".env"),
    str(_BACKEND / "data" / "encryption.key"),
    str(_BACKEND / "branding_uploads" / "logo.csv"),
    str(_BACKEND / "logs" / "app.csv"),
    str(_BACKEND / "alembic" / "env.py"),
    str(_BACKEND / "tests" / "integrations" / "integrations.json"),
    str(_BACKEND.parent / "bow-config.yaml"),
    str(_BACKEND.parent / "certs" / "ca.pem"),
    "/etc/passwd",
    "/proc/self/environ",
    "/root/.ssh/id_rsa",
    "/root/.aws/credentials",
    "/sys/kernel/notes",
    "/dev/stdin",
    "/run/secrets/db_password",
    "/var/run/secrets/kubernetes.io/serviceaccount/token",
    str(Path.home() / ".ssh" / "id_rsa"),
    str(Path.home() / ".aws" / "credentials"),
]


@pytest.mark.parametrize("path", _PROTECTED)
def test_path_policy_refuses_app_and_system_locations(path):
    with pytest.raises(ValueError):
        lockdown.check_local_path(path)
    for conn_type, config in (
        ("sqlite", {"database": path}),
        ("duckdb", {"database": path}),
        ("duckdb", {"uris": path}),
        ("csv", {"file_paths": path}),
        ("qvd", {"file_paths": path}),
        ("pbix", {"file_paths": path}),
    ):
        with pytest.raises(ValueError):
            lockdown.check_connection_config(conn_type, config)


def test_path_policy_accepts_a_normal_data_folder_and_bundled_samples(data, tmp_path):
    lockdown.check_local_path(str(data / "sales.csv"))
    lockdown.check_local_path(str(data), contains=True)
    lockdown.check_connection_config("csv", {"file_paths": f"{data}/*.csv\n{data}/sub/**/*.csv"})
    lockdown.check_connection_config("duckdb", {"uris": f"{data}/*.parquet\ns3://bucket/prefix/*.parquet"})
    lockdown.check_connection_config("sqlite", {"database": str(tmp_path / "shop.sqlite")})
    lockdown.check_connection_config("sqlite", {"database": ":memory:"})
    # Shipped demo databases and test fixtures live inside the app tree.
    lockdown.check_connection_config("sqlite", {"database": "demo-datasources/chinook.sqlite"})
    lockdown.check_connection_config("duckdb", {"database": str(_BACKEND / "demo-datasources" / "stocks.duckdb")})
    lockdown.check_connection_config("csv", {"file_paths": str(_BACKEND / "tests" / "config" / "*.csv")})
    # Ordinary places a deployment keeps or mounts data, including a folder
    # of the operator's own next to the app's.
    for path in ("/var/lib/warehouse/sales.sqlite", "/usr/share/datasets/sales.sqlite", "/data/sales.sqlite",
                 "/mnt/share/sales.sqlite", "/srv/sales.sqlite", "/opt/data/sales.sqlite", "/run/media/sales.sqlite",
                 "/root/data/sales.sqlite",
                 # The compose / chart data volume, and the folders that hold the
                 # app database and key: a customer file there is the customer's.
                 str(_BACKEND / "uploads" / "sales.sqlite"), str(_BACKEND / "uploads" / "warehouse" / "sales.sqlite"),
                 str(_BACKEND / "db" / "sales.sqlite"), str(_BACKEND / "data" / "sales.sqlite"),
                 str(_BACKEND / "customer-data" / "sales.sqlite"), str(_BACKEND.parent / "datasets" / "sales.sqlite")):
        lockdown.check_connection_config("sqlite", {"database": path})
        lockdown.check_connection_config("duckdb", {"uris": path.replace("sales.sqlite", "sales.parquet")})
        lockdown.check_connection_config("csv", {"file_paths": path.replace("sales.sqlite", "*.csv")})
        if Path(path).parent != _BACKEND / "uploads":
            # (A whole-folder DuckDB session at the top of uploads/ would
            # take in the app's own sub-folders; the csv connector, which
            # opens files one by one, works there.)
            lockdown.check_connection_config("duckdb", {"uris": path.replace("sales.sqlite", "*.parquet")})
    # Types with no local path are not this policy's business.
    lockdown.check_connection_config("postgresql", {"host": "db", "database": "/etc/passwd"})


def test_path_policy_refuses_a_folder_that_contains_the_app(tmp_path):
    for pattern in ("/*.parquet", "/**/*.csv", f"{_BACKEND.parent}/**/*.parquet"):
        with pytest.raises(ValueError):
            lockdown.check_connection_config("duckdb", {"uris": pattern})


def test_path_policy_follows_symlinks(tmp_path):
    link = tmp_path / "innocent.sqlite"
    link.symlink_to("/etc/passwd")
    with pytest.raises(ValueError):
        lockdown.check_local_path(str(link))
    assert SqliteClient(database=str(link)).test_connection()["success"] is False


def test_allowed_roots_setting_is_a_strict_allowlist(data, tmp_path, monkeypatch):
    from app.settings.config import settings

    monkeypatch.setattr(settings.bow_config.security, "file_connector_allowed_roots", [str(data)])
    lockdown.check_local_path(str(data / "sales.csv"))
    with pytest.raises(ValueError):
        lockdown.check_local_path(str(tmp_path / "elsewhere" / "private.csv"))
    with pytest.raises(ValueError):
        lockdown.check_local_path(str(_BACKEND / "db" / "app.db"))
    lockdown.check_local_path(str(_BACKEND / "demo-datasources" / "chinook.sqlite"))


def test_generated_demo_data_is_reachable_only_by_its_own_organization():
    """Generated demo datasets live in one folder per organization."""
    from app.core import request_org
    from app.services.demo_data import installer

    mine, theirs = "0b9d6c1e-1111-4222-8333-444455556666", "7a1f0e2d-9999-4888-8777-666655554444"
    my_file = installer.dataset_path(mine, "ds1")
    their_file = installer.dataset_path(theirs, "ds1")

    # The connection's owner is known: only its own folder.
    lockdown.check_local_path(my_file, organization_id=mine)
    with pytest.raises(ValueError):
        lockdown.check_local_path(their_file, organization_id=mine)
    with pytest.raises(ValueError):
        lockdown.check_local_path(str(Path(my_file).parent / ".." / theirs / "ds1.sqlite"), organization_id=mine)

    # Nobody is known: no organization's folder.
    with pytest.raises(ValueError):
        lockdown.check_local_path(my_file)

    # Inside a request the caller's organization is the owner.
    token = request_org._organization_id.set(mine)
    try:
        lockdown.check_connection_config("sqlite", {"database": my_file})
        with pytest.raises(ValueError):
            lockdown.check_connection_config("sqlite", {"database": their_file})
    finally:
        request_org._organization_id.reset(token)

    # A client built for a connection carries its organization to connect time.
    client = lockdown.bind_organization(SqliteClient(database=their_file), mine)
    assert client.test_connection()["success"] is False
    assert "protected" in client.test_connection()["message"]


@pytest.mark.parametrize("uri", [
    "https://files.example.com/reports/../private/x.csv",
    "s3://bucket/prefix/../other/*.parquet",
    "https://files.example.com/reports/%2e%2e/private/x.csv",
    "https://files.example.com/reports/..%2fprivate/x.csv",
    "az://container/data/..",
])
def test_remote_uri_may_not_step_out_of_its_prefix(uri):
    with pytest.raises(ValueError):
        lockdown.check_connection_config("duckdb", {"uris": uri})
    client = DuckDBClient(uris="https://files.example.com/reports/sales.csv")
    for sql in (f"SELECT * FROM read_csv_auto('{uri}')", f"SELECT * FROM '{uri}'", f"TABLE '{uri}'"):
        with pytest.raises(ReadOnlyViolation):
            ensure_read_only(client, sql)
    # As data, the same text is just a string.
    ensure_read_only(client, f"SELECT * FROM sales WHERE source = '{uri}'")


def test_remote_uri_prefixes_are_built_from_the_configured_patterns():
    paths, dirs = lockdown.sources_for_patterns(
        ["s3://bucket/sales/2024/*.parquet", "https://files.example.com/reports/sales.csv"]
    )
    assert dirs == ["s3://bucket/sales/2024/"]
    assert paths == ["https://files.example.com/reports/sales.csv"]
    ensure_read_only(DuckDBClient(uris="s3://bucket/sales/*.parquet"),
                     "SELECT * FROM sales WHERE note LIKE '%..%' AND url = 'https://example.com/a/b'")


def test_policy_is_enforced_when_connecting_not_only_when_saving():
    assert SqliteClient(database="/etc/passwd").test_connection()["success"] is False
    assert DuckDBClient(database="/etc/passwd").test_connection()["success"] is False
    assert DuckDBClient(uris="/etc/passwd").test_connection()["success"] is False
    with pytest.raises(Exception):
        DuckDBClient(uris="/etc/*").execute_query("SELECT 1")


def test_glob_does_not_pick_up_a_link_into_a_protected_location(data):
    (data / "linked.csv").symlink_to("/etc/passwd")
    client = CSVClient(file_paths=str(data / "*.csv"))
    assert sorted(t.name for t in client.get_tables()) == ["returns", "sales"]


# -- SQLite -------------------------------------------------------------------


def test_sqlite_file_is_opened_read_only(tmp_path):
    path = _sqlite_file(tmp_path / "shop.sqlite")
    client = SqliteClient(database=str(path))
    with client.connect() as conn:
        for sql in ("INSERT INTO orders (total) VALUES (99)", "DELETE FROM orders",
                    "CREATE TABLE extra (a)", "DROP TABLE orders"):
            with pytest.raises(sqlite3.Error):
                conn.execute(sql)
                conn.commit()
        with pytest.raises(sqlite3.Error):
            conn.execute(f"ATTACH DATABASE '{tmp_path / 'second.sqlite'}' AS second")
    assert not (tmp_path / "second.sqlite").exists()
    assert client.execute_query("SELECT COUNT(*) AS n FROM orders")["n"][0] == 2


def test_sqlite_missing_database_is_reported_clearly_and_not_created(tmp_path):
    missing = tmp_path / "missing.sqlite"
    client = SqliteClient(database=str(missing))
    result = client.test_connection()
    assert result["success"] is False
    assert "not found" in result["message"] and str(missing) in result["message"]
    with pytest.raises(Exception, match="not found"):
        client.execute_query("SELECT 1")
    assert not missing.exists()


def test_sqlite_extraction_handle_is_read_only_too(tmp_path):
    """Custom-table extraction and FK reflection open a second handle."""
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    client = SqliteClient(database=str(_sqlite_file(tmp_path / "shop.sqlite")))
    with client.extraction_connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM orders")).scalar() == 2
        with pytest.raises(SQLAlchemyError):
            conn.execute(text("INSERT INTO orders (total) VALUES (99)"))
            conn.commit()
    assert client.execute_query("SELECT COUNT(*) AS n FROM orders")["n"][0] == 2
    with pytest.raises(Exception):
        with SqliteClient(database="/etc/passwd").extraction_connect():
            pass


@pytest.mark.parametrize("sql", [
    "PRAGMA writable_schema(1)",
    "PRAGMA journal_mode(WAL)",
    "PRAGMA main.user_version(7)",
    "PRAGMA writable_schema = 1",
])
def test_sqlite_pragma_that_sets_a_value_is_refused(sql, tmp_path):
    client = SqliteClient(database=str(_sqlite_file(tmp_path / "shop.sqlite")))
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(client, sql)
    with client.connect() as conn:
        with pytest.raises(sqlite3.Error):
            conn.execute(sql).fetchall()


@pytest.mark.parametrize("sql", [
    "PRAGMA table_info(orders)",
    'PRAGMA table_info("orders")',
    "PRAGMA foreign_key_list(orders)",
    "PRAGMA index_list(orders)",
    "PRAGMA table_list",
    "PRAGMA user_version",
])
def test_sqlite_pragma_that_describes_an_object_still_works(sql, tmp_path):
    client = SqliteClient(database=str(_sqlite_file(tmp_path / "shop.sqlite")))
    ensure_read_only(client, sql)
    client.execute_query(sql)


# -- escape hatch -------------------------------------------------------------


def test_unsafe_setting_restores_unrestricted_duckdb_sessions(unrestricted, data, outside):
    client = CSVClient(file_paths=str(data / "*.csv"))
    sql = f"SELECT content FROM read_text('{outside / 'private.txt'}')"
    ensure_read_only(client, sql)
    ensure_read_only(client, f"COPY sales TO '{outside / 'copy.csv'}'")
    assert client.execute_query(sql)["content"][0] == "not part of any connection"

    duck = DuckDBClient(uris=str(data / "*.csv"))
    with duck.connect() as con:
        assert con.execute(f"SELECT COUNT(*) FROM read_csv_auto('{outside / 'private.csv'}')").fetchone()[0] == 1
        con.execute("SET threads = 2")


def test_unsafe_setting_restores_path_and_sqlite_behaviour(unrestricted, tmp_path):
    lockdown.check_local_path("/etc/passwd")
    lockdown.check_connection_config("csv", {"file_paths": "/etc/*.csv"})
    client = SqliteClient(database=str(_sqlite_file(tmp_path / "shop.sqlite")))
    with client.connect() as conn:
        conn.execute("INSERT INTO orders (total) VALUES (1)")
        conn.commit()
    assert client.execute_query("SELECT COUNT(*) AS n FROM orders")["n"][0] == 3


_DUCKDB_BASED = ["duckdb_file", "duckdb_uris", "csv", "qvd", "pbix", "pbirs", "iceberg"]


def _duckdb_based_client(kind, data, tmp_path, monkeypatch, request):
    """(client, send, table): `send(sql)` is how the agent's code reaches
    that client's engine (the wrapper's check, then the client)."""
    from tests.unit import test_duckdb_file_lockdown as files
    from tests.unit.test_lakehouse_clients import _ice

    if kind == "duckdb_file":
        client, table = DuckDBClient(database=str(_duckdb_file(_folder(tmp_path / "db") / "shop.duckdb"))), "prices"
    elif kind == "duckdb_uris":
        client, table = DuckDBClient(uris=str(data / "sales.csv")), "sales"
    elif kind == "iceberg":
        client, table = _ice(request.getfixturevalue("iceberg_catalog")), "sales.orders"
    elif kind == "pbirs":
        report = files._PbirsReport(tmp_path, monkeypatch)
        return report.client, lambda sql: report.execute_query(ensure_read_only(report.client, sql)), "sales"
    else:
        client = {"csv": lambda: files._csv(tmp_path), "qvd": lambda: files._qvd(tmp_path, monkeypatch),
                  "pbix": lambda: files._pbix(tmp_path, monkeypatch)}[kind]()
        table = "sales"
    return client, lambda sql: client.execute_query(ensure_read_only(client, sql)), table


@pytest.mark.parametrize("kind", _DUCKDB_BASED)
def test_with_the_unsafe_setting_off_every_duckdb_based_client_is_locked_and_checked(
        kind, data, outside, tmp_path, monkeypatch, request):
    from app.settings.config import settings

    monkeypatch.setattr(settings.bow_config.security, "unsafe_allow_unrestricted_file_connectors", False)
    client, send, table = _duckdb_based_client(kind, data, tmp_path, monkeypatch, request)
    locked = []
    real_lock = lockdown.lock_connection

    def lock_and_read_back(con, **kwargs):
        real_lock(con, **kwargs)
        locked.append(con.execute(
            "SELECT current_setting('lock_configuration'), current_setting('enable_external_access')").fetchone())

    monkeypatch.setattr(lockdown, "lock_connection", lock_and_read_back)
    assert len(send(f"SELECT * FROM {table}")) >= 1
    assert locked and set(locked) == {(True, False)}
    secret = str(outside / "private.csv")
    for sql in (f"SELECT * FROM read_csv_auto('{secret}')", f"SELECT * FROM '{secret}'",
                f"SELECT content FROM read_text('{outside / 'private.txt'}')",
                f"COPY {table} TO '{outside / 'copy.csv'}'", "SELECT current_setting('threads') AS t"):
        with pytest.raises((ReadOnlyViolation, TableScopeViolation)):
            send(sql)
    assert not (outside / "copy.csv").exists()


def test_settings_default_to_the_restricted_behaviour():
    from app.settings.bow_config import SecurityConfig

    cfg = SecurityConfig()
    assert cfg.unsafe_allow_unrestricted_file_connectors is False
    assert cfg.file_connector_allowed_roots is None


# -- the app's own SQLite database --------------------------------------------


@pytest.fixture
def app_database(monkeypatch):
    """Point the configured app database at a file; returns a setter."""
    from app.settings.config import settings

    def _set(path: Path) -> Path:
        monkeypatch.setattr(settings.bow_config.database, "url", f"sqlite:///{path}")
        return path

    return _set


def test_app_database_on_a_shared_data_volume_protects_the_file_not_the_folder(data, app_database):
    """The app database may sit on the volume the customer's data is on."""
    db = app_database(data / "app.db")
    _sqlite_file(db)
    _sqlite_file(data / "shop.sqlite")

    # Customer data next to it keeps working.
    lockdown.check_connection_config("sqlite", {"database": str(data / "shop.sqlite")})
    lockdown.check_connection_config("csv", {"file_paths": str(data / "*.csv")})
    lockdown.check_connection_config("duckdb", {"uris": str(data / "*.parquet")})
    lockdown.check_connection_config("duckdb", {"uris": str(data / "sales.csv")})
    assert CSVClient(file_paths=str(data / "*.csv")).execute_query("SELECT COUNT(*) AS n FROM sales")["n"][0] == 2
    assert DuckDBClient(uris=str(data / "*.csv")).test_connection()["success"] is True
    assert SqliteClient(database=str(data / "shop.sqlite")).test_connection()["success"] is True

    # The database itself, its sidecars, and any pattern that can match them.
    for name in ("app.db", "app.db-wal", "app.db-shm", "app.db-journal"):
        for conn_type, key in (("sqlite", "database"), ("duckdb", "database"), ("duckdb", "uris"), ("csv", "file_paths")):
            with pytest.raises(ValueError):
                lockdown.check_connection_config(conn_type, {key: str(data / name)})
    for pattern in ("*", "*.db", "app.*", "a??.db", "*.db-wal", f"../{data.name}/*.db"):
        with pytest.raises(ValueError):
            lockdown.check_connection_config("csv", {"file_paths": str(data / pattern)})
    # (A duckdb URI ending in .db names one database file, it is not expanded.)
    for pattern in ("*", "app.*", "a??.d?", "*.db-wal", f"../{data.name}/app*"):
        with pytest.raises(ValueError):
            lockdown.check_connection_config("duckdb", {"uris": str(data / pattern)})
        assert DuckDBClient(uris=str(data / pattern)).test_connection()["success"] is False
    with pytest.raises(ValueError):
        lockdown.check_connection_config("duckdb", {"uris": str(data.parent / "**" / "app.d*")})
    assert SqliteClient(database=str(db)).test_connection()["success"] is False


def test_app_database_in_its_default_folder_protects_the_file_not_the_folder(app_database):
    db = app_database(_BACKEND / "db" / "app.db")
    for name in ("app.db", "app.db-wal", "app.db-shm", "app.db-journal"):
        with pytest.raises(ValueError):
            lockdown.check_local_path(str(db.with_name(name)))
    for pattern in ("*", "*.db", "app.*"):
        with pytest.raises(ValueError):
            lockdown.check_connection_config("csv", {"file_paths": str(db.parent / pattern)})
    lockdown.check_local_path(str(db.parent / "customer.sqlite"))
    lockdown.check_connection_config("csv", {"file_paths": str(db.parent / "*.csv")})


def test_encryption_key_is_protected_as_a_file(monkeypatch, tmp_path):
    for name in ("encryption.key", ".encryption.key.lock", ".encryption.key.Ab12Cd"):
        with pytest.raises(ValueError):
            lockdown.check_local_path(str(_BACKEND / "data" / name))
    with pytest.raises(ValueError):
        lockdown.check_connection_config("duckdb", {"uris": str(_BACKEND / "data" / "*")})
    lockdown.check_connection_config("duckdb", {"uris": str(_BACKEND / "data" / "*.parquet")})

    elsewhere = tmp_path / "keys" / "bow.key"
    monkeypatch.setenv("BOW_ENCRYPTION_KEY_FILE", str(elsewhere))
    with pytest.raises(ValueError):
        lockdown.check_local_path(str(elsewhere))
    lockdown.check_local_path(str(elsewhere.parent / "sales.csv"))


# -- directories assigned to one organization ---------------------------------


@pytest.fixture
def departments(tmp_path, monkeypatch):
    """Two department shares; organization A is assigned the first."""
    from app.settings.config import settings

    share_a, share_b = tmp_path / "deptA", tmp_path / "deptB"
    for share in (share_a, share_b):
        share.mkdir()
        (share / "report.csv").write_text("k,v\nx,1\n")
    org_a, org_b = "11111111-aaaa-4aaa-8aaa-111111111111", "22222222-bbbb-4bbb-8bbb-222222222222"
    monkeypatch.setattr(settings.bow_config.security, "organization_file_roots", {org_a: [str(share_a)]})
    return {"a": share_a, "b": share_b, "org_a": org_a, "org_b": org_b}


def _as_request_of(organization_id):
    from contextlib import contextmanager

    from app.core import request_org

    @contextmanager
    def scope():
        token = request_org._organization_id.set(organization_id)
        try:
            yield
        finally:
            request_org._organization_id.reset(token)

    return scope()


def test_organization_with_assigned_roots_stays_inside_them(departments):
    from app.data_sources.clients.network_dir_client import NetworkDirClient, check_network_dir_root

    a, b, org_a = departments["a"], departments["b"], departments["org_a"]
    with _as_request_of(org_a):
        check_network_dir_root(str(a))
        lockdown.check_connection_config("csv", {"file_paths": str(a / "*.csv")})
        lockdown.check_connection_config("sqlite", {"database": str(a / "x.sqlite")})
        lockdown.check_connection_config("duckdb", {"uris": str(a / "*.parquet")})
        with pytest.raises(ValueError):
            check_network_dir_root(str(b))
        for conn_type, config in (("csv", {"file_paths": str(b / "*.csv")}),
                                  ("sqlite", {"database": str(b / "x.sqlite")}),
                                  ("duckdb", {"uris": str(b / "*.parquet")}),
                                  ("qvd", {"file_paths": str(b / "*.qvd")}),
                                  ("pbix", {"file_paths": str(b / "*.pbix")})):
            with pytest.raises(ValueError):
                lockdown.check_connection_config(conn_type, config)
        # Shared sample data is not a customer path.
        lockdown.check_connection_config("sqlite", {"database": "demo-datasources/chinook.sqlite"})

    # The same holds at connect time, for a client built for that organization.
    inside = lockdown.bind_organization(NetworkDirClient(root_path=str(a)), org_a)
    outside = lockdown.bind_organization(NetworkDirClient(root_path=str(b)), org_a)
    assert inside.test_connection()["success"] is True
    assert outside.test_connection()["success"] is False
    assert lockdown.bind_organization(CSVClient(file_paths=str(a / "*.csv")), org_a).get_tables()


def test_assigned_directory_is_reserved_for_its_organization(departments):
    """Another organization, or one nobody identified, cannot point into it;
    everywhere else it behaves as before."""
    from app.data_sources.clients.network_dir_client import NetworkDirClient, check_network_dir_root

    reserved, free = departments["a"], departments["b"]
    for organization_id in (departments["org_b"], None):
        with _as_request_of(organization_id):
            check_network_dir_root(str(free))
            lockdown.check_connection_config("csv", {"file_paths": str(free / "*.csv")})
            lockdown.check_connection_config("sqlite", {"database": "demo-datasources/chinook.sqlite"})
            with pytest.raises(ValueError):
                check_network_dir_root(str(reserved))
            for conn_type, config in (("csv", {"file_paths": str(reserved / "*.csv")}),
                                      ("sqlite", {"database": str(reserved / "x.sqlite")}),
                                      ("duckdb", {"uris": str(reserved / "report.csv")}),
                                      ("duckdb", {"database": str(reserved / "x.duckdb")})):
                with pytest.raises(ValueError):
                    lockdown.check_connection_config(conn_type, config)
        # Connect time, with no request around: the client's own binding decides.
        assert lockdown.bind_organization(
            NetworkDirClient(root_path=str(free)), organization_id).test_connection()["success"] is True
        assert lockdown.bind_organization(
            NetworkDirClient(root_path=str(reserved)), organization_id).test_connection()["success"] is False
        assert lockdown.bind_organization(CSVClient(file_paths=str(reserved / "*.csv")), organization_id).get_tables() == []
        assert lockdown.bind_organization(
            DuckDBClient(uris=str(reserved / "*.csv")), organization_id).test_connection()["success"] is False
        assert lockdown.bind_organization(
            SqliteClient(database=str(reserved / "x.sqlite")), organization_id).test_connection()["success"] is False

    # Its own organization, bound on the client, with no request around.
    org_a = departments["org_a"]
    assert lockdown.bind_organization(DuckDBClient(uris=str(reserved / "*.csv")), org_a).test_connection()["success"] is True
    assert [t.name for t in lockdown.bind_organization(CSVClient(file_paths=str(reserved / "*.csv")), org_a).get_tables()] == ["report"]
    assert lockdown.bind_organization(DuckDBClient(uris=str(free / "*.csv")), org_a).test_connection()["success"] is False
    assert lockdown.bind_organization(CSVClient(file_paths=str(free / "*.csv")), org_a).get_tables() == []


def test_no_assigned_roots_means_no_change(departments, monkeypatch):
    from app.data_sources.clients.network_dir_client import check_network_dir_root
    from app.settings.config import settings

    monkeypatch.setattr(settings.bow_config.security, "organization_file_roots", {})
    with _as_request_of(departments["org_a"]):
        check_network_dir_root(str(departments["b"]))
        lockdown.check_connection_config("csv", {"file_paths": str(departments["b"] / "*.csv")})


def test_organization_file_roots_setting_is_validated_when_loaded():
    from pydantic import ValidationError

    from app.settings.bow_config import SecurityConfig

    assert SecurityConfig().organization_file_roots == {}
    assert SecurityConfig(organization_file_roots=None).organization_file_roots == {}
    ok = SecurityConfig(organization_file_roots={"org-1": ["/mnt/deptA", "/srv/a"]})
    assert ok.organization_file_roots == {"org-1": ["/mnt/deptA", "/srv/a"]}
    for bad in ({"org-1": "/mnt/deptA"}, {"org-1": ["relative/dir"]}, {"": ["/mnt"]}, ["/mnt"], {"org-1": [5]}):
        with pytest.raises(ValidationError):
            SecurityConfig(organization_file_roots=bad)


# -- a URI connection: the path policy runs on what the session is locked to ----


_POLICY_REFUSAL = "(?i)protected|another organization"


@pytest.fixture
def guarded_tree(tmp_path, monkeypatch):
    """A folder holding, below it, a protected directory (the system logs
    folder, set the way an operator sets it) and a protected file (the
    encryption key), each readable as CSV; and a folder of the connection's
    own data beside it."""
    from app.settings.config import settings

    top = tmp_path / "top"
    logs, keys, own = top / "logs", top / "keys", tmp_path / "own"
    for folder in (logs, keys, own):
        folder.mkdir(parents=True)
    (logs / "server.csv").write_text("line\nhidden-log\n")
    key = keys / "key.csv"
    key.write_text("k\nhidden-key\n")
    (own / "sales.csv").write_text("region,amount\nnorth,10\n")
    monkeypatch.setattr(settings.bow_config.system_logs, "directory", str(logs))
    monkeypatch.setenv("BOW_ENCRYPTION_KEY_FILE", str(key))
    return SimpleNamespace(top=top, logs=logs, keys=keys, key=key, own=own)


def _refused_at_connect_with_nothing_read(client, watched: dict) -> None:
    """`client.connect()` is refused by the path policy, and no file named
    in `watched` ({directory: file name}) is opened."""
    from tests.unit.test_duckdb_parse_time_access import _Opens

    probes = {folder: _Opens(folder) for folder in watched}
    try:
        with pytest.raises(RuntimeError, match=_POLICY_REFUSAL):
            with client.connect():
                pass
        for folder, probe in probes.items():
            assert watched[folder] not in probe()
    finally:
        for probe in probes.values():
            probe.close()


@pytest.mark.parametrize("shape", ["{top}", "{top}/", "{keys}", "{keys}/"])
def test_a_directory_uri_above_a_protected_location_is_refused_at_connect_with_nothing_read(shape, guarded_tree):
    client = DuckDBClient(uris=shape.format(top=guarded_tree.top, keys=guarded_tree.keys))
    _refused_at_connect_with_nothing_read(client, {guarded_tree.logs: "server.csv", guarded_tree.keys: "key.csv"})


@pytest.mark.parametrize("shape", ["file://localhost{logs}/*.csv", "file://localhost{logs}/", "file://localhost{key}",
                                   "file://localhost{keys}/*.csv", "file://localhost{top}",
                                   "file://localhost{top}/**/*.csv"])
def test_a_localhost_file_uri_is_held_to_the_path_it_names(shape, guarded_tree):
    uri = shape.format(logs=guarded_tree.logs, key=guarded_tree.key, keys=guarded_tree.keys, top=guarded_tree.top)
    _refused_at_connect_with_nothing_read(
        DuckDBClient(uris=uri), {guarded_tree.logs: "server.csv", guarded_tree.keys: "key.csv"})
    with pytest.raises(ValueError, match=_POLICY_REFUSAL):
        lockdown.check_connection_config("duckdb", {"uris": uri})


def test_a_glob_is_checked_against_protected_files_under_its_directory_as_the_engine_resolves_it(
        guarded_tree, monkeypatch):
    # DuckDB reads `~name` as the home directory's text followed by the name:
    # `~eys` under the home directory `top/k` is `top/keys`.
    home = _folder(guarded_tree.top / "k")
    monkeypatch.setenv("HOME", str(home))
    _refused_at_connect_with_nothing_read(DuckDBClient(uris="~eys/*.csv"), {guarded_tree.keys: "key.csv"})


def test_a_file_uri_names_a_local_path_only_with_no_host_or_localhost():
    assert lockdown.local_path_of("file:///data/x.csv") == "/data/x.csv"
    assert lockdown.local_path_of("file://localhost/data/x.csv") == "/data/x.csv"
    assert lockdown.local_path_of("file:/data/x.csv") == "/data/x.csv"
    assert lockdown.local_path_of("/data/x.csv") == "/data/x.csv"
    # Another host, a host spelled other than DuckDB reads it as local, or
    # no path at all: not a local file.
    for uri in ("file://fileserver/data/x.csv", "file://LOCALHOST/data/x.csv", "file://localhost", "file://"):
        with pytest.raises(ValueError):
            lockdown.local_path_of(uri)
    with pytest.raises(ValueError):
        lockdown.check_connection_config("duckdb", {"uris": "file://fileserver/data/x.csv"})


@pytest.mark.parametrize("shape", ["{parent}", "{parent}/", "{parent}/**/*.csv", "file://localhost{parent}/**/*.csv"])
def test_a_directory_or_deep_glob_above_another_organizations_root_is_refused(shape, departments):
    """From an organization with no roots of its own, a directory that
    holds another organization's root is refused, as one inside it is."""
    from app.data_sources.clients._duckdb_lockdown import bind_organization

    reserved, org_b = departments["a"], departments["org_b"]
    parent = reserved.parent
    client = bind_organization(DuckDBClient(uris=shape.format(parent=parent)), org_b)
    _refused_at_connect_with_nothing_read(client, {reserved: "report.csv"})
    with pytest.raises(ValueError, match=_POLICY_REFUSAL):
        lockdown.check_local_path(str(parent), contains=True, organization_id=org_b)
    # A file connector's glob over it is refused when it is saved.
    with _as_request_of(org_b), pytest.raises(ValueError, match=_POLICY_REFUSAL):
        lockdown.check_connection_config("csv", {"file_paths": f"{parent}/**/*.csv"})
    # A database file there is served without its folder.
    db = _duckdb_file(parent / "market.duckdb")
    with bind_organization(DuckDBClient(database=str(db)), org_b).connect() as session:
        assert session.execute("SELECT count(*) FROM prices").fetchone()[0] == 1
        with pytest.raises(duckdb.Error, match="(?i)disabled by configuration"):
            session.execute(f"SELECT * FROM read_csv_auto('{reserved / 'report.csv'}')").fetchall()


def test_a_network_directory_root_above_another_organizations_root_is_refused_with_nothing_read(departments):
    """From an organization with no roots of its own, a network directory
    root that holds another organization's root is refused when it is saved
    and when it connects; a root over its own data keeps working."""
    from fastapi import HTTPException

    from app.data_sources.clients._duckdb_lockdown import bind_organization
    from app.data_sources.clients.network_dir_client import NetworkDirClient
    from app.services.connection_service import _check_network_dir_root
    from tests.unit.test_duckdb_parse_time_access import _Opens

    reserved, free, org_b = departments["a"], departments["b"], departments["org_b"]
    parent = reserved.parent
    with _as_request_of(org_b), pytest.raises(HTTPException) as refused:
        _check_network_dir_root("network_dir", {"root_path": str(parent)})
    assert "another organization" in str(refused.value.detail)

    client = bind_organization(NetworkDirClient(root_path=str(parent)), org_b)
    opens = _Opens(reserved)
    try:
        assert client.test_connection()["success"] is False
        with pytest.raises(ValueError, match="another organization"):
            client.list_files()
        with pytest.raises(ValueError, match="another organization"):
            client.read_file(f"{reserved.name}/report.csv")
        assert "report.csv" not in opens()
    finally:
        opens.close()

    # A root over the organization's own data.
    with _as_request_of(org_b):
        _check_network_dir_root("network_dir", {"root_path": str(free)})
    own = bind_organization(NetworkDirClient(root_path=str(free)), org_b)
    assert own.test_connection()["success"] is True
    assert own.read_file("report.csv")["k"].tolist() == ["x"]


def test_uris_over_the_connections_own_data_still_connect_and_read(guarded_tree, http_base, monkeypatch):
    own = guarded_tree.own
    for uri in (str(own), f"{own}/", f"file://{own}/*.csv", f"file://localhost{own}/*.csv", f"{http_base}/remote.csv"):
        client = DuckDBClient(uris=uri)
        with client.connect() as con:
            assert con.execute(f"SELECT count(*) FROM {client.view_names()[0]}").fetchone()[0] == 1, uri
    # Object-store prefixes are locked to as listed (no view is created:
    # nothing is fetched).
    monkeypatch.setattr(DuckDBClient, "_create_views", lambda self, con: [])
    client = DuckDBClient(uris=f"{own}/*.csv\ns3://bucket/exports/*.parquet\nhttps://files.example.com/r/x.csv")
    with client.connect() as con:
        directories = con.execute("SELECT current_setting('allowed_directories')").fetchone()[0]
        paths = con.execute("SELECT current_setting('allowed_paths')").fetchone()[0]
    assert "s3://bucket/exports/" in directories
    assert "https://files.example.com/r/x.csv" in paths


def test_a_uri_is_refused_when_the_directory_the_engine_resolves_it_to_is_protected(guarded_tree, monkeypatch):
    # DuckDB reads `~ogs` as the home directory's text followed by `ogs`:
    # under the home directory `top/l` that is the protected `top/logs`.
    monkeypatch.setenv("HOME", str(_folder(guarded_tree.top / "l")))
    _refused_at_connect_with_nothing_read(DuckDBClient(uris="~ogs/*.csv"), {guarded_tree.logs: "server.csv"})


# -- the path policy runs on the path that is opened -----------------------------


@pytest.fixture
def linked_roots(tmp_path, monkeypatch):
    """Two organizations with a root each, side by side. Organization A's
    root holds a link `l` to a folder two levels down, so `<A>/l/../..` is
    A's root itself, while the same text read without the link is the
    folder holding both roots."""
    from app.settings.config import settings

    root_a, root_c = tmp_path / "rootA", tmp_path / "rootC"
    _folder(root_a / "x" / "y")
    _folder(root_c)
    (root_a / "l").symlink_to(root_a / "x" / "y")
    (root_c / "report.csv").write_text("k,v\ntheirs,1\n")
    org_a, org_c = "33333333-aaaa-4aaa-8aaa-333333333333", "44444444-cccc-4ccc-8ccc-444444444444"
    monkeypatch.setattr(settings.bow_config.security, "organization_file_roots",
                        {org_a: [str(root_a)], org_c: [str(root_c)]})
    return SimpleNamespace(a=root_a, c=root_c, org_a=org_a, org_c=org_c, through_link=f"{root_a}/l/../../{root_c.name}")


@pytest.mark.parametrize("form", ["file:{path}", "file://{path}", "file://localhost{path}"])
@pytest.mark.parametrize("place", ["another organization's root", "a protected folder"])
def test_a_database_file_named_with_a_file_prefix_is_checked_at_the_path_it_names(form, place, departments, guarded_tree):
    folder, organization = ((departments["a"], departments["org_b"]) if place == "another organization's root"
                            else (guarded_tree.logs, departments["org_b"]))
    db = _duckdb_file(folder / "x.duckdb")
    value = form.format(path=db)
    with _as_request_of(organization), pytest.raises(ValueError, match=_POLICY_REFUSAL):
        lockdown.check_connection_config("duckdb", {"database": value})
    for client in (DuckDBClient(database=value), DuckDBClient(uris=value)):
        _refused_at_connect_with_nothing_read(lockdown.bind_organization(client, organization), {folder: "x.duckdb"})


def test_a_database_file_of_the_organizations_own_is_still_opened_whatever_its_form(departments):
    db = _duckdb_file(departments["b"] / "market.duckdb")
    for value in (str(db), f"file:{db}", f"file://{db}", f"file://localhost{db}"):
        with _as_request_of(departments["org_b"]):
            lockdown.check_connection_config("duckdb", {"database": value})
        for client in (DuckDBClient(database=value), DuckDBClient(uris=value)):
            with lockdown.bind_organization(client, departments["org_b"]).connect() as session:
                assert session.execute("SELECT count(*) FROM prices").fetchone()[0] == 1, value


def _database_with_views(path: Path, theirs: Path) -> Path:
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE prices AS SELECT 'acme' AS ticker, 12.5 AS price")
    con.execute(f"CREATE VIEW theirs AS SELECT * FROM read_csv_auto('{theirs}')")
    con.close()
    return path


@pytest.mark.parametrize("shape", ["database", "uri to a file", "uri to a glob"])
def test_a_database_file_session_is_locked_only_to_locations_the_policy_checked(shape, linked_roots):
    """A database file in organization A's root with a view over
    organization C's file, named through A's link: the session may not
    read C's file, and nothing in C's root is opened."""
    from tests.unit.test_duckdb_parse_time_access import _Opens

    db = _database_with_views(linked_roots.a / "market.duckdb", linked_roots.c / "report.csv")
    if shape == "database":
        client = DuckDBClient(database=f"{linked_roots.a}/l/../../market.duckdb")
    elif shape == "uri to a file":
        client = DuckDBClient(database=str(db), uris=f"{linked_roots.through_link}/report.csv")
    else:
        client = DuckDBClient(database=str(db), uris=f"{linked_roots.through_link}/*.csv")
    client = lockdown.bind_organization(client, linked_roots.org_a)
    opens = _Opens(linked_roots.c)
    try:
        assert client.execute_query("SELECT count(*) AS n FROM prices")["n"][0] == 1
        with pytest.raises(Exception, match="(?i)disabled by configuration") as refused:
            client.execute_query("SELECT * FROM theirs")
        assert "theirs,1" not in str(refused.value)
        assert "report.csv" not in opens()
    finally:
        opens.close()


def test_a_sqlite_file_is_opened_at_the_path_the_policy_checked(linked_roots):
    from tests.unit.test_duckdb_parse_time_access import _Opens

    theirs = linked_roots.c / "x.sqlite"
    con = sqlite3.connect(theirs)
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, total REAL)")
    con.execute("INSERT INTO orders (total) VALUES (999.0)")
    con.commit()
    con.close()
    # Through A's link the same text names a file in A's own root.
    own = _sqlite_file(_folder(linked_roots.a / linked_roots.c.name) / "x.sqlite")
    client = lockdown.bind_organization(SqliteClient(database=f"{linked_roots.through_link}/x.sqlite"),
                                        linked_roots.org_a)
    opens = _Opens(linked_roots.c)
    try:
        assert sorted(client.execute_query("SELECT total FROM orders")["total"].tolist()) == [5.0, 7.5]
        own.unlink()
        with pytest.raises(RuntimeError, match="not found"):
            client.execute_query("SELECT total FROM orders")
        assert "x.sqlite" not in opens()
    finally:
        opens.close()
    # A plain path to the organization's own file.
    plain = _sqlite_file(linked_roots.a / "plain.sqlite")
    client = lockdown.bind_organization(SqliteClient(database=str(plain)), linked_roots.org_a)
    assert client.test_connection()["success"] is True


def test_a_file_connection_reads_each_file_its_globs_found_where_the_policy_checked_it(guarded_tree, tmp_path,
                                                                                       monkeypatch):
    """A folder literally named `~ogs` in the working directory holds a
    file named like the protected log. The glob finds that file, and that
    file is what the connection reads, not the protected log the engine
    would read for the same text (`~ogs` as the home directory's text
    followed by `ogs`)."""
    from tests.unit.test_duckdb_parse_time_access import _Opens

    work = _folder(tmp_path / "work")
    (_folder(work / "~ogs") / "server.csv").write_text("line\nown-copy\n")
    monkeypatch.chdir(work)
    monkeypatch.setenv("HOME", str(_folder(guarded_tree.top / "l")))
    lockdown.check_connection_config("csv", {"file_paths": "~ogs/*.csv"})
    assert lockdown.filter_files(["~ogs/server.csv"], "csv") == [str(work / "~ogs" / "server.csv")]
    opens = _Opens(guarded_tree.logs)
    try:
        assert CSVClient(file_paths="~ogs/*.csv").execute_query("SELECT * FROM server")["line"].tolist() == ["own-copy"]
        assert "server.csv" not in opens()
    finally:
        opens.close()


@pytest.mark.parametrize("client_cls, ext", [(QVDClient, ".qvd"), (PBIXClient, ".pbix")])
def test_a_converted_file_is_cached_under_the_file_the_policy_checked(client_cls, ext, linked_roots):
    """A file named through organization A's link is A's own file: its
    converted copy is cached under that file, never under organization C's
    file that the same text names without the link."""
    theirs = linked_roots.c / f"x{ext}"
    theirs.write_bytes(b"theirs")
    own = _folder(linked_roots.a / linked_roots.c.name) / f"x{ext}"
    own.write_bytes(b"own")
    through_link = f"{linked_roots.through_link}/x{ext}"
    assert client_cls._cache_key(through_link) == client_cls._cache_key(str(own))
    assert client_cls._cache_key(through_link)[0] != client_cls._cache_key(str(theirs))[0]


def test_a_tls_file_option_is_checked_where_the_driver_opens_it(tmp_path, monkeypatch):
    """Database drivers open a TLS file option as written: `~` is a folder
    name to them, not the home directory."""
    from app.data_sources.clients._sa_url import path_allowed
    from app.settings.config import settings

    work, home = _folder(tmp_path / "work"), _folder(tmp_path / "home")
    monkeypatch.setattr(settings.bow_config.system_logs, "directory", str(_folder(work / "~")))
    monkeypatch.chdir(work)
    monkeypatch.setenv("HOME", str(home))
    assert path_allowed("~/client.pem") is False
    assert path_allowed(str(home / "client.pem")) is True


@pytest.mark.parametrize("value", ["sqlite:///file:{db}?uri=true", "sqlite:///file://localhost{db}?mode=ro&uri=true",
                                   "sqlite:///file:{db}"])
def test_an_iceberg_catalog_database_is_named_by_its_path(value, departments):
    from app.data_sources.clients.iceberg_client import checked_extra_properties

    reserved, free, org_b = departments["a"], departments["b"], departments["org_b"]
    with _as_request_of(org_b):
        with pytest.raises(ValueError, match="'uri'"):
            checked_extra_properties({"type": "sql", "uri": value.format(db=reserved / "catalog.db")})
        own = f"sqlite:///{free}/catalog.db"
        assert checked_extra_properties({"type": "sql", "uri": own, "warehouse": f"file://{free}"})["uri"] == own


def test_an_iceberg_location_is_checked_where_pyiceberg_opens_it(linked_roots):
    from app.data_sources.clients.iceberg_client import checked_extra_properties

    with _as_request_of(linked_roots.org_a):
        # PyIceberg reads a location with no scheme as its absolute form,
        # `..` removed as text: `<A>/l/../../rootC` is C's root to it.
        for value in (linked_roots.through_link, f"file://localhost{linked_roots.a}"):
            with pytest.raises(ValueError, match="'warehouse'"):
                checked_extra_properties({"type": "sql", "warehouse": value})
        assert checked_extra_properties({"type": "sql", "warehouse": f"file://{linked_roots.a}"})


# -- what a statement may name -------------------------------------------------


def _relation_bypasses(path: str) -> list[str]:
    """Every way of making the engine read ``path`` (a file or URL) that is
    not a plain table name."""
    return [
        f"SELECT * FROM '{path}'",
        f"FROM '{path}'",
        f"TABLE '{path}'",
        f"SUMMARIZE '{path}'",
        f"DESCRIBE '{path}'",
        f"DESCRIBE SELECT * FROM '{path}'",
        f"SUMMARIZE SELECT * FROM '{path}'",
        f"EXPLAIN ANALYZE SELECT * FROM '{path}'",
        f"SELECT * FROM E'{path}'",
        f"SELECT * FROM $${path}$$",
        f'SELECT * FROM "{path}"',
        f"PIVOT '{path}' ON s USING count(*)",
        f"SELECT * FROM sales POSITIONAL JOIN '{path}'",
        f"SELECT * FROM sales, '{path}'",
        f"SELECT * FROM sales,/**/'{path}'",
        f"SELECT * FROM sales NATURAL JOIN/*x*/'{path}'",
        f"VALUES ((SELECT s FROM '{path}'))",
        f"SELECT * FROM sales WHERE amount IN (SELECT s FROM '{path}')",
        f"SELECT * FROM read_csv('{path}')",
        f"SELECT * FROM READ_CSV('{path}')",
        f"SELECT * FROM main.read_csv('{path}')",
        f"SELECT * FROM system.main.read_csv('{path}')",
        f"SELECT * FROM \"read_csv\"('{path}')",
        f"SELECT * FROM (FROM read_csv('{path}'))",
        f"WITH t AS (SELECT * FROM read_csv('{path}')) SELECT * FROM t",
        f"SELECT * FROM read_text('{path}')",
        f"SELECT content FROM read_blob('{path}')",
        f"SELECT * FROM sniff_csv('{path}')",
        f"SELECT * FROM parquet_scan('{path}')",
        f"SELECT * FROM glob('{path}')",
    ]


_STATE_READS = [
    "SELECT current_setting('s3_secret_access_key')",
    "SELECT current_setting/**/('s3_secret_access_key')",
    "SELECT \"current_setting\"('s3_secret_access_key')",
    "SELECT main.current_setting('s3_secret_access_key')",
    "SELECT current_setting\n('s3_secret_access_key')",
    "SELECT current_setting --x\n('s3_secret_access_key')",
    "SELECT CURRENT_SETTING('allowed_directories')",
    "SELECT * FROM duckdb_settings() WHERE name LIKE 's3_secret%'",
    "SELECT value FROM duckdb_settings/**/() WHERE name = 's3_secret_access_key'",
    "SELECT * FROM \"duckdb_settings\"()",
    "SELECT * FROM duckdb_secrets()",
    "SELECT * FROM duckdb_secrets/**/()",
    "SELECT * FROM which_secret('s3://x/y', 's3')",
    "SELECT getenv('HOME')",
    "SELECT getvariable('x')",
    "SHOW ALL",  # fine on its own; kept here with its neighbours below
    "SELECT * FROM duckdb_databases()",
    "SELECT sql FROM duckdb_views() WHERE NOT internal",
    "SELECT * FROM duckdb_functions() LIMIT 1",
    "SELECT * FROM duckdb_extensions()",
    "SELECT * FROM duckdb_logs LIMIT 1",
    "SELECT * FROM duckdb_memory()",
    "SELECT * FROM duckdb_temporary_files()",
    "SELECT * FROM pragma_user_agent()",
    "SELECT * FROM pragma_database_list()",
    "SELECT * FROM query('SELECT 42')",
    "SELECT * FROM query_table('sales')",
    "SHOW DATABASES",
    "CALL pragma_version()",
    "CALL enable_logging()",
    "PREPARE p AS SELECT * FROM read_csv($1)",
    "SET VARIABLE x = '/etc/hostname'",
    "CREATE MACRO m(x) AS TABLE SELECT * FROM read_csv(x)",
    "CREATE SECRET s (TYPE s3, KEY_ID 'a', SECRET 'b')",
    "SELECT * FROM sales; SELECT 2",
]
_STATE_READS.remove("SHOW ALL")

_MUST_PASS = [
    "SELECT * FROM sales",
    "SELECT region, SUM(amount) AS total FROM sales GROUP BY ALL ORDER BY total DESC LIMIT 5",
    "SELECT s.region, r.amount FROM sales s LEFT JOIN returns r USING (region)",
    "SELECT * FROM main.sales",
    "WITH t AS (SELECT * FROM sales WHERE amount > 5) SELECT COUNT(*) FROM t",
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT * FROM r",
    "SELECT * FROM (SELECT region FROM sales) x WHERE region IN (SELECT region FROM returns)",
    "SELECT region, row_number() OVER (PARTITION BY region ORDER BY amount) AS rn FROM sales QUALIFY rn = 1",
    "PIVOT sales ON region USING sum(amount)",
    "SELECT * FROM (PIVOT sales ON region USING sum(amount))",
    "UNPIVOT sales ON amount INTO NAME k VALUE v",
    "DESCRIBE sales",
    "DESCRIBE SELECT * FROM sales",
    "SHOW sales",
    "SUMMARIZE sales",
    "SUMMARIZE SELECT * FROM sales",
    "SHOW TABLES",
    "SHOW ALL TABLES",
    "SELECT * FROM (SHOW ALL TABLES)",
    "EXPLAIN SELECT * FROM sales",
    "EXPLAIN ANALYZE SELECT COUNT(*) FROM sales",
    "PRAGMA show_tables",
    "PRAGMA table_info('sales')",
    "SELECT * FROM pragma_table_info('sales')",
    "SELECT list_transform([1, 2], x -> x + 1), {'a': 1}.a, [x * 2 FOR x IN [1, 2]], struct_pack(k := region) FROM sales",
    "SELECT unnest([1, 2]) AS x, region FROM sales",
    "SELECT * FROM sales, unnest([1, 2]) AS u(x)",
    "SELECT * FROM range(3) t(i), generate_series(1, 2) g(j)",
    "SELECT * FROM generate_series(DATE '2024-01-01', DATE '2024-01-03', INTERVAL 1 DAY)",
    "SELECT column_name FROM duckdb_columns() WHERE table_name = 'sales'",
    "SELECT table_name FROM duckdb_tables()",
    "SELECT table_name FROM information_schema.tables",
    "SELECT column_name FROM information_schema.columns WHERE table_name = 'sales'",
    "SELECT * FROM sales USING SAMPLE 1",
    "FROM sales SELECT region",
    "SELECT * FROM sales AS s(a, b)",
    # Strings that look like files, URLs or traversal are only strings.
    "SELECT * FROM sales WHERE region = 'https://x.com/a%2Fb'",
    "SELECT * FROM sales WHERE region LIKE '%../%'",
    "SELECT 'https://a/b?x=1' AS link, '/etc/passwd' AS note, 's3://b/../c?s3_endpoint=h' AS s FROM sales",
    "SELECT * FROM sales WHERE region IN ('a.csv', '/tmp/x.parquet', 'https://h/p/../q')",
    "SELECT replace(region, '../', ''), concat('s3://', region, '/x.csv'), length('read_csv(''x'')') FROM sales",
    "SELECT * FROM sales WHERE region = 'current_setting(''x'')' OR region = 'duckdb_settings'",
]


def test_every_way_of_naming_a_file_or_url_as_a_relation_is_refused(data, outside):
    client = CSVClient(file_paths=str(data / "*.csv"))
    targets = [
        str(outside / "private.csv"), "/etc/hostname", "private.csv", "../elsewhere/private.csv",
        str(data / "sales.csv"),  # even the connection's own file: tables are queried by name
        "https://example.com/x.csv", "s3://bkt/org1/x.csv", "s3://bkt/org1/../org2/x.csv",
        "s3://bkt/org1/x.csv?s3_endpoint=evil.example.com",
        "s3://bkt/org1/x.csv?s3_endpoint=127.0.0.1:9&s3_use_ssl=false",
    ]
    for target in targets:
        for sql in _relation_bypasses(target):
            with pytest.raises(ReadOnlyViolation):
                check_sql(sql, "duckdb")
            with pytest.raises(Exception):
                client.execute_query(sql)
            with client.connect() as con:
                with pytest.raises(ReadOnlyViolation):
                    lockdown.check_session_sql(con, sql)
            # The file connection's own dispatch check refuses every form.
            _refused_where_checked(client, sql)


@pytest.mark.parametrize("sql", _STATE_READS)
def test_session_state_is_not_readable(sql, data):
    for client in (CSVClient(file_paths=str(data / "*.csv")),
                   DuckDBClient(uris=str(data / "*.csv"), access_key="AKIAEXAMPLE", secret_key="SK_SECRET_VALUE")):
        _refused_where_checked(client, sql)
        with pytest.raises(Exception):
            client.execute_query(sql)


# What a file connection does not take, though DuckDB reads it: DESCRIBE,
# SHOW, SUMMARIZE, EXPLAIN, PRAGMA, catalog views and table functions, and
# PIVOT / UNPIVOT as a whole statement. Each is refused at its dispatch point
# before it runs (as a statement, as a table, or with an error the agent fixes
# by rewriting the query).
_NOT_ON_FILE_CONNECTIONS = [
    "PIVOT sales ON region USING sum(amount)",
    "SELECT * FROM (PIVOT sales ON region USING sum(amount))",
    "UNPIVOT sales ON amount INTO NAME k VALUE v",
    "DESCRIBE sales",
    "DESCRIBE SELECT * FROM sales",
    "SHOW sales",
    "SUMMARIZE sales",
    "SUMMARIZE SELECT * FROM sales",
    "SHOW TABLES",
    "SHOW ALL TABLES",
    "SELECT * FROM (SHOW ALL TABLES)",
    "EXPLAIN SELECT * FROM sales",
    "EXPLAIN ANALYZE SELECT COUNT(*) FROM sales",
    "PRAGMA show_tables",
    "PRAGMA table_info('sales')",
    "SELECT * FROM pragma_table_info('sales')",
    "SELECT column_name FROM duckdb_columns() WHERE table_name = 'sales'",
    "SELECT table_name FROM duckdb_tables()",
    "SELECT table_name FROM information_schema.tables",
    "SELECT column_name FROM information_schema.columns WHERE table_name = 'sales'",
]


@pytest.mark.parametrize("sql", [q for q in _MUST_PASS if q not in _NOT_ON_FILE_CONNECTIONS])
def test_ordinary_queries_keep_working(sql, data):
    client = CSVClient(file_paths=str(data / "*.csv"))
    ensure_read_only(client, sql)
    check_sql(sql, "duckdb")
    client.execute_query(sql)


@pytest.mark.parametrize("sql", _NOT_ON_FILE_CONNECTIONS)
def test_reads_a_file_connection_does_not_take_are_refused_before_they_run(sql, data):
    assert sql in _MUST_PASS
    _refused_where_checked(CSVClient(file_paths=str(data / "*.csv")), sql)


def test_unknown_table_still_gets_the_engines_own_error(data):
    """A mistyped table name passes the wrapper's check. A file connection
    then refuses it at its dispatch point, naming its own tables, so the
    agent sees what it can query."""
    client = CSVClient(file_paths=str(data / "*.csv"))
    ensure_read_only(client, "SELECT * FROM salez")
    # A file connection names its own tables in the refusal instead.
    with pytest.raises(ConnectionTableViolation, match="returns, sales.*salez is not one of the tables"):
        client.execute_query("SELECT * FROM salez")


def test_storage_credentials_are_out_of_reach_of_sql(data):
    """The access key signs requests; no statement can read it back."""
    secret = "SK_SECRET_VALUE_9f3"
    client = DuckDBClient(uris=str(data / "*.csv"), access_key="AKIAEXAMPLE", secret_key=secret,
                          region="eu-west-1", session_token="TOKEN_VALUE_77", connection_string=None)
    with client.connect() as con:
        # Asked directly, past every guard:
        settings = con.execute("SELECT name, value FROM duckdb_settings()").fetchall()
        assert not [n for n, v in settings if secret in str(v) or "TOKEN_VALUE_77" in str(v)]
        assert secret not in str(con.execute("SELECT current_setting('s3_secret_access_key')").fetchall())
        shown = str(con.execute("SELECT * FROM duckdb_secrets()").fetchall())
        assert "bow_s3" in shown and secret not in shown and "TOKEN_VALUE_77" not in shown
        with pytest.raises(duckdb.Error):
            con.execute("SELECT * FROM duckdb_secrets(redact = false)").fetchall()
        with pytest.raises(duckdb.Error):
            con.execute("SET allow_unredacted_secrets = true")
        assert con.execute("SELECT COUNT(*) FROM data").fetchone()[0] == 3


def test_object_store_requests_are_signed_with_the_configured_key(tmp_path):
    """Same key, region and session token on the wire as before."""
    import http.server
    import threading

    seen = []

    class Capture(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):
            seen.append((self.headers.get("Authorization") or "", self.headers.get("x-amz-security-token")))
            self.send_response(404)
            self.end_headers()

        do_GET = do_HEAD

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = DuckDBClient(uris="s3://bucket/reports/sales.parquet", access_key="AKIAEXAMPLE",
                              secret_key="SK_SECRET_VALUE", region="eu-west-1", session_token="TOKEN_VALUE")
        con = duckdb.connect(":memory:")
        client._configure_httpfs(con)
        con.execute(f"SET s3_endpoint='127.0.0.1:{server.server_address[1]}'")
        con.execute("SET s3_use_ssl=false")
        con.execute("SET http_retries=0")
        with pytest.raises(duckdb.Error):
            con.execute("SELECT * FROM read_parquet('s3://bucket/reports/sales.parquet')").fetchall()
        con.close()
    finally:
        server.shutdown()
    assert seen, "the request reached the object store endpoint"
    authorization, token = seen[0]
    assert "Credential=AKIAEXAMPLE/" in authorization and "/eu-west-1/s3/" in authorization
    assert token == "TOKEN_VALUE"


@pytest.mark.parametrize("uri", [
    "s3://bucket/data/x.parquet?s3_endpoint=evil.example.com",
    "s3://bucket/data/*.parquet?s3_use_ssl=false",
    "gs://bucket/data/x.csv?s3_endpoint=h",
    "az://container/x.csv#frag=1",
    "https://files.example.com/x.csv?s3_endpoint=evil.example.com",
])
def test_configured_uri_may_not_redirect_the_request(uri):
    with pytest.raises(ValueError):
        lockdown.check_connection_config("duckdb", {"uris": uri})
    assert DuckDBClient(uris=uri).test_connection()["success"] is False


def test_configured_uri_keeps_globs_and_signed_https_links():
    lockdown.check_connection_config("duckdb", {"uris": "s3://bucket/data/part-??.parquet"})
    lockdown.check_connection_config("duckdb", {"uris": "s3://bucket/data/[ab]*.parquet"})
    lockdown.check_connection_config(
        "duckdb", {"uris": "https://acct.blob.core.windows.net/c/x.csv?sv=2022-11-02&sig=abc%3D&se=2030-01-01"})
    lockdown.check_connection_config(
        "duckdb", {"uris": "https://bucket.s3.amazonaws.com/x.parquet?X-Amz-Signature=abc&X-Amz-Expires=600"})


# -- a database file and the files its views read ------------------------------


def test_database_file_views_over_files_in_its_folder_keep_working(tmp_path, outside, data):
    folder = _folder(tmp_path / "warehouse")
    pd.DataFrame({"sku": ["a", "b"], "qty": [3, 4]}).to_parquet(folder / "stock.parquet")
    _folder(folder / "archive")
    pd.DataFrame({"sku": ["z"], "qty": [1]}).to_parquet(folder / "archive" / "old.parquet")
    far = _folder(tmp_path / "far")
    pd.DataFrame({"sku": ["q"], "qty": [9]}).to_parquet(far / "other.parquet")
    listed = _folder(tmp_path / "listed")
    pd.DataFrame({"sku": ["l"], "qty": [5]}).to_parquet(listed / "extra.parquet")

    db = folder / "market.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE prices AS SELECT 'acme' AS ticker, 12.5 AS price")
    con.execute(f"CREATE VIEW stock AS SELECT * FROM read_parquet('{folder / 'stock.parquet'}')")
    con.execute(f"CREATE VIEW archive AS SELECT * FROM read_parquet('{folder / 'archive' / '*.parquet'}')")
    con.execute(f"CREATE VIEW far_away AS SELECT * FROM read_parquet('{far / 'other.parquet'}')")
    con.execute(f"CREATE VIEW extra AS SELECT * FROM read_parquet('{listed / 'extra.parquet'}')")
    con.close()

    client = DuckDBClient(database=str(db))
    assert client.execute_query("SELECT SUM(qty) AS n FROM stock")["n"][0] == 7
    assert client.execute_query("SELECT SUM(qty) AS n FROM archive")["n"][0] == 1
    assert client.execute_query("SELECT COUNT(*) AS n FROM prices")["n"][0] == 1
    assert {t.name for t in client.get_tables()} >= {"prices", "stock", "archive"}

    # A view over a file somewhere else: refused, and the message says what
    # was refused and how such a location is allowed.
    with pytest.raises(Exception) as refused:
        client.execute_query("SELECT * FROM far_away")
    message = str(refused.value)
    assert str(far / "other.parquet") in message
    assert "URIs" in message and "unsafe_allow_unrestricted_file_connectors" in message

    # ... which is: list the location on the connection.
    listed_client = DuckDBClient(database=str(db), uris=str(listed / "*.parquet"))
    assert listed_client.execute_query("SELECT SUM(qty) AS n FROM extra")["n"][0] == 5
    with pytest.raises(Exception):
        listed_client.execute_query("SELECT * FROM far_away")

    # The session is still sealed, and still read-only.
    with client.connect() as session:
        for sql in (f"SELECT * FROM read_text('{outside / 'private.txt'}')", "INSTALL spatial",
                    "SET enable_external_access = true", "CREATE TABLE t AS SELECT 1 AS a",
                    f"COPY prices TO '{far / 'out.csv'}'"):
            with pytest.raises(duckdb.Error):
                session.execute(sql)


def test_a_database_file_session_reads_its_folder_and_uris_inside_its_organizations_roots_only(
        tmp_path, departments):
    """On the session itself, with no text check in the way: the engine
    reads the database's folder and the connection's URIs, and nothing
    outside them or outside the organization's roots."""
    from app.data_sources.clients._duckdb_lockdown import bind_organization

    share, org = departments["a"], departments["org_a"]
    folder = _folder(share / "warehouse")
    pd.DataFrame({"qty": [3, 4]}).to_parquet(folder / "stock.parquet")
    listed = _folder(share / "listed")
    pd.DataFrame({"qty": [5]}).to_parquet(listed / "extra.parquet")
    far = _folder(share / "far")
    pd.DataFrame({"qty": [9]}).to_parquet(far / "other.parquet")
    foreign = _folder(tmp_path / "foreign")
    pd.DataFrame({"qty": [7]}).to_parquet(foreign / "theirs.parquet")
    db = _duckdb_file(folder / "market.duckdb")

    client = bind_organization(DuckDBClient(database=str(db), uris=f"{listed}/*.parquet"), org)
    with client.connect() as session:
        assert session.execute("SELECT count(*) FROM prices").fetchone()[0] == 1
        assert session.execute(f"SELECT sum(qty) FROM read_parquet('{folder / 'stock.parquet'}')").fetchone()[0] == 7
        assert session.execute(f"SELECT sum(qty) FROM read_parquet('{listed / '*.parquet'}')").fetchone()[0] == 5
        for path in (far / "other.parquet", foreign / "theirs.parquet", departments["b"] / "report.csv"):
            with pytest.raises(duckdb.Error, match="(?i)disabled by configuration"):
                session.execute(f"SELECT * FROM read_csv_auto('{path}')").fetchall()

    # A URI outside the organization's roots is not added to what the
    # session may read, even though the connection lists it.
    client = bind_organization(DuckDBClient(database=str(db), uris=f"{foreign}/*.parquet"), org)
    with client.connect() as session, pytest.raises(duckdb.Error, match="(?i)disabled by configuration"):
        session.execute(f"SELECT * FROM read_parquet('{foreign / 'theirs.parquet'}')").fetchall()


def test_a_database_file_engine_locked_for_other_uris_is_refused_and_one_locked_the_same_way_is_taken(tmp_path):
    """Connections to one database file share an engine: a second one is
    taken only when the engine reads back exactly its own folder and URIs."""
    folder = _folder(tmp_path / "warehouse")
    listed = _folder(tmp_path / "listed")
    pd.DataFrame({"qty": [5]}).to_parquet(listed / "extra.parquet")
    db = _duckdb_file(folder / "market.duckdb")
    plain = DuckDBClient(database=str(db))
    with plain.connect() as first:
        with DuckDBClient(database=str(db)).connect() as same:
            assert same.execute("SELECT count(*) FROM prices").fetchone()[0] == 1
        with (pytest.raises(RuntimeError, match="Refusing to query files without the engine locked") as refused,
              DuckDBClient(database=str(db), uris=f"{listed}/*.parquet").connect()):
            pass
        # What the user can act on, and no path.
        message = str(refused.value)
        assert "another open connection to the same database file uses different additional locations" in message
        assert "use the same additional locations" in message
        assert str(tmp_path) not in message
        assert first.execute("SELECT count(*) FROM prices").fetchone()[0] == 1


def test_in_memory_sqlite_cannot_attach_a_file(tmp_path):
    other = _sqlite_file(tmp_path / "other.sqlite")
    with SqliteClient(database=":memory:").connect() as conn:
        with pytest.raises(sqlite3.Error):
            conn.execute(f"ATTACH DATABASE '{other}' AS o")


# -- relations with unusual names ----------------------------------------------


@pytest.fixture
def odd_names_db(tmp_path):
    """A database file whose schemas and tables are not plain identifiers."""
    db = _folder(tmp_path / "odd") / "My Data.duckdb"
    con = duckdb.connect(str(db))
    con.execute('CREATE SCHEMA "Sales Ops"')
    con.execute('CREATE SCHEMA "sales-ops"')
    con.execute('CREATE TABLE "Sales Ops".orders AS SELECT 1 AS id')
    con.execute('CREATE TABLE "sales-ops".orders AS SELECT 2 AS id')
    con.execute('CREATE TABLE "sales-ops"."Order Lines" AS SELECT 3 AS id')
    con.execute('CREATE TABLE "export.csv" AS SELECT 4 AS id')
    con.execute('CREATE TABLE "2026 plan/v2" AS SELECT 5 AS id')
    con.execute('CREATE VIEW "s3://not-a-bucket/x.parquet" AS SELECT 6 AS id')
    con.execute('CREATE TABLE "Sales Ops"."data.parquet" AS SELECT 7 AS id')
    con.close()
    (db.parent / "real.csv").write_text("id\n99\n")
    return db


_ODD_NAME_QUERIES = [
    ('SELECT id FROM "Sales Ops".orders', 1),
    ('SELECT id FROM "sales-ops".orders', 2),
    ('SELECT id FROM "SALES OPS".ORDERS', 1),
    ('SELECT o.id FROM "Sales Ops".orders o JOIN "sales-ops".orders p ON o.id < p.id', 1),
    ('SELECT id FROM "sales-ops"."Order Lines"', 3),
    ('SELECT id FROM "sales-ops"."order lines"', 3),
    ('SELECT id FROM "My Data"."Sales Ops".orders', 1),
    ('SELECT id FROM "my data"."sales-ops"."Order Lines"', 3),
    ('SELECT id FROM "export.csv"', 4),
    ('SELECT id FROM "EXPORT.CSV"', 4),
    ('SELECT id FROM main."export.csv"', 4),
    ('SELECT id FROM "My Data".main."export.csv"', 4),
    ('SELECT id FROM "2026 plan/v2"', 5),
    ('SELECT id FROM "s3://not-a-bucket/x.parquet"', 6),
    ('SELECT id FROM "Sales Ops"."data.parquet"', 7),
    ('SELECT id FROM (SELECT id FROM "Sales Ops".orders) AS "sub query"', 1),
]


@pytest.mark.parametrize("sql,expected", _ODD_NAME_QUERIES)
def test_existing_relations_are_queryable_whatever_their_names(sql, expected, odd_names_db):
    client = DuckDBClient(database=str(odd_names_db))
    ensure_read_only(client, sql)
    assert client.execute_query(sql)["id"][0] == expected


def test_describe_show_and_explain_work_on_oddly_named_relations(odd_names_db):
    client = DuckDBClient(database=str(odd_names_db))
    for sql in ('DESCRIBE "Sales Ops".orders', 'SHOW "sales-ops"."Order Lines"',
                'EXPLAIN SELECT * FROM "2026 plan/v2"'):
        ensure_read_only(client, sql)
        assert len(client.execute_query(sql)) >= 1


@pytest.mark.parametrize("sql,refusal", [
    ('SUMMARIZE "export.csv"', ReadOnlyViolation),
    ('PIVOT "export.csv" ON id USING count(*)', ReadOnlyViolation),
    # A CTE name that is not plain letters, digits and underscores is an
    # error the agent fixes by renaming the CTE.
    ('WITH "my cte.csv" AS (SELECT id FROM "export.csv") SELECT id FROM "my cte.csv"', DuckdbQueryError),
])
def test_what_a_duckdb_connection_does_not_take_on_oddly_named_relations_is_refused_before_it_runs(
        sql, refusal, odd_names_db):
    """The names are the database's own; SUMMARIZE, a PIVOT statement and a
    CTE whose name is not plain are not taken on a DuckDB connection (see the
    reads a file connection does not take, above)."""
    with pytest.raises(refusal):
        ensure_read_only(DuckDBClient(database=str(odd_names_db)), sql)


def _quoted(name) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def test_a_quoted_path_like_name_the_database_does_not_hold_is_refused(odd_names_db):
    client = DuckDBClient(database=str(odd_names_db))
    for name in ("missing.csv", "plans/2027.parquet", "s3://not-a-bucket/missing.parquet"):
        sql = f"SELECT * FROM {_quoted(name)}"
        # The wrapper has no session catalog: it leaves relation names to
        # the client, whose own check refuses the name.
        assert ensure_read_only(client, sql) == sql
        with pytest.raises(ReadOnlyViolation):
            client.execute_query(sql)


def test_a_quoted_name_that_is_a_file_beside_the_database_is_refused_without_opening_it(odd_names_db):
    """`real.csv` sits in the database's folder, which the connection may
    read; the name is not one of the database's relations, so it is refused
    before it runs and the file is never opened."""
    from tests.unit.test_duckdb_parse_time_access import _Opens

    client = DuckDBClient(database=str(odd_names_db))
    sql = f"SELECT * FROM {_quoted(odd_names_db.parent / 'real.csv')}"
    opens = _Opens(odd_names_db.parent)
    try:
        (odd_names_db.parent / "real.csv").read_text()
        assert "real.csv" in opens()
        # The wrapper leaves relation names to the client (no session
        # catalog there); the client's own check refuses the name.
        assert ensure_read_only(client, sql) == sql
        with pytest.raises(ReadOnlyViolation) as refused:
            client.execute_query(sql)
        assert "99" not in str(refused.value)
        assert "real.csv" not in opens()
    finally:
        opens.close()


def test_on_its_own_the_database_file_client_refuses_a_cte_whose_name_is_not_plain(odd_names_db):
    """The client's own check, with no wrapper in front of it: a statement
    defining a CTE whose name is not plain is refused before it runs,
    whatever else the statement names, and no file in the database's folder
    is opened."""
    from tests.unit.test_duckdb_parse_time_access import _Opens

    client = DuckDBClient(database=str(odd_names_db))
    name = _quoted(odd_names_db.parent / "real.csv")
    sql = f"SELECT b.id FROM (WITH {name} AS (SELECT 1 AS id) SELECT id FROM {name}) AS a, {name} AS b"
    opens = _Opens(odd_names_db.parent)
    try:
        with pytest.raises(ReadOnlyViolation) as refused:
            client.execute_query(sql)
        assert "99" not in str(refused.value)
        assert "real.csv" not in opens()
    finally:
        opens.close()
    assert client.execute_query('WITH mine AS (SELECT id FROM "export.csv") SELECT id FROM mine')["id"][0] == 4


def test_the_database_file_catalog_matches_names_as_the_engine_folds_them(tmp_path):
    """A table whose name holds a letter outside ASCII is matched the way
    the engine matches it: only its ASCII letters fold. A spelling that is
    the same name only under a wider folding is not that table, and is
    refused before it runs; no file in the database's folder is opened."""
    from tests.unit.test_duckdb_parse_time_access import _Opens

    folder = _folder(tmp_path / "kdb")
    (folder / "data.csv").write_text("id\n99\n")
    db = folder / "store.duckdb"
    con = duckdb.connect(str(db))
    odd = str(folder / "data.csv").replace("/kdb/", "/" + chr(0x212A) + "db/")
    con.execute(f"CREATE TABLE {_quoted(odd)} AS SELECT 1 AS id")
    con.close()

    client = DuckDBClient(database=str(db))
    assert client.execute_query(f"SELECT id FROM {_quoted(odd)}")["id"][0] == 1
    opens = _Opens(folder)
    try:
        with pytest.raises(ReadOnlyViolation) as refused:
            client.execute_query(f"SELECT id FROM {_quoted(folder / 'data.csv')}")
        assert "99" not in str(refused.value)
        assert "data.csv" not in opens()
    finally:
        opens.close()


def test_with_the_text_checks_off_the_engine_still_stops_a_quoted_name_outside_the_folder(odd_names_db, monkeypatch):
    """The engine lock, not the text check: a quoted name that is a file
    outside the database's folder and its URIs cannot be read."""
    beside = _folder(odd_names_db.parent.parent / "beside")
    (beside / "private.csv").write_text("id\n42\n")
    monkeypatch.setattr(lockdown, "check_user_sql", lambda *a, **k: None)
    monkeypatch.setattr(lockdown, "check_session_sql", lambda *a, **k: None)
    client = DuckDBClient(database=str(odd_names_db))
    assert client.execute_query('SELECT id FROM "export.csv"')["id"][0] == 4
    with pytest.raises(Exception, match="(?i)disabled by configuration|permission") as refused:
        client.execute_query(f"SELECT * FROM {_quoted(beside / 'private.csv')}")
    assert "42" not in str(refused.value)


@pytest.mark.parametrize("sql", [
    # Not in the catalog: a file next to the database, by any spelling.
    'SELECT * FROM "real.csv"',
    "SELECT * FROM 'real.csv'",
    'SELECT * FROM "{folder}/real.csv"',
    'SELECT * FROM "Sales Ops"."real.csv"',
    'SELECT * FROM "no such schema".orders',
    'SELECT * FROM "My Data"."no-such"."export.csv"',
    'SELECT * FROM "/etc/hostname"',
    'SELECT * FROM "https://example.com/x.csv"',
    'SELECT * FROM "s3://not-a-bucket/y.parquet"',
    'SELECT * FROM "export.csv.gz"',
    'SUMMARIZE "real.csv"',
    'PIVOT "real.csv" ON id USING count(*)',
    # A real relation, but written as a string where one is ambiguous with a file.
    "SELECT * FROM '{folder}/real.csv'",
    "SELECT * FROM read_csv('{folder}/real.csv')",
])
def test_names_that_are_not_relations_are_still_refused(sql, odd_names_db):
    client = DuckDBClient(database=str(odd_names_db))
    with pytest.raises(Exception) as refused:
        client.execute_query(sql.format(folder=odd_names_db.parent))
    assert "99" not in str(refused.value)
    with client.connect() as con:
        with pytest.raises(ReadOnlyViolation):
            lockdown.check_session_sql(con, sql.format(folder=odd_names_db.parent))


def test_a_string_in_a_relation_position_is_refused_even_for_a_real_table(odd_names_db):
    """`FROM 'export.csv'` is how a file is named; the table is `"export.csv"`."""
    client = DuckDBClient(database=str(odd_names_db))
    with pytest.raises(Exception):
        client.execute_query("SELECT id FROM 'export.csv'")
    assert client.execute_query('SELECT id FROM "export.csv"')["id"][0] == 4


_NON_ASCII_BEFORE_A_RELATION = [
    """SELECT 'é' AS v FROM "$orders" """,
    """SELECT '日本語' AS v FROM "$orders" """,
    """SELECT 'é' AS v /* ü */ FROM "Sales Ops".orders JOIN "$orders" ON true""",
]


@pytest.mark.parametrize("sql", _NON_ASCII_BEFORE_A_RELATION)
def test_non_ascii_text_before_a_relation_does_not_shift_where_it_is_read(sql, odd_names_db):
    """The engine reports a relation's position in bytes; 'é' is two."""
    con = duckdb.connect(str(odd_names_db))
    con.execute('CREATE TABLE "$orders" AS SELECT 8 AS id')
    con.close()
    client = DuckDBClient(database=str(odd_names_db))
    ensure_read_only(client, sql)
    assert len(client.execute_query(sql)) == 1


@pytest.mark.parametrize("sql", [
    "SELECT 'ééé' AS v, * FROM '{folder}/real.csv'",
    "SELECT 'é' AS v, * FROM $${folder}/real.csv$$",
    "SELECT '日本語' AS v, * FROM E'{folder}/real.csv'",
])
def test_non_ascii_text_does_not_hide_a_string_in_a_relation_position(sql, odd_names_db):
    client = DuckDBClient(database=str(odd_names_db))
    with pytest.raises(Exception) as refused:
        client.execute_query(sql.format(folder=odd_names_db.parent))
    assert "99" not in str(refused.value)
    with client.connect() as con:
        with pytest.raises(ReadOnlyViolation):
            lockdown.check_session_sql(con, sql.format(folder=odd_names_db.parent))


def test_a_relation_position_is_read_as_a_byte_offset():
    sql = """SELECT 'é' FROM "$orders" """
    at = sql.encode("utf-8").index(b'"$orders"')
    assert at != sql.index('"$orders"')
    assert lockdown._written_as_string(sql, at) is False
    quoted = "SELECT 'é' FROM '/x'"
    assert lockdown._written_as_string(quoted, quoted.encode("utf-8").index(b"'/x'")) is True


def test_file_named_views_are_queryable_under_the_listed_name(data, tmp_path):
    """The schema listing and the query agree on the name, for files whose
    names are not identifiers."""
    (data / "Q1 sales-2026.csv").write_text("region,amount\neast,5\n")
    (data / "2026.plan.csv").write_text("region,amount\nwest,6\n")
    csv = CSVClient(file_paths=str(data / "*.csv"))
    names = {t.name for t in csv.get_tables()}
    assert {"q1_sales_2026", "2026_plan"} <= names
    for name in names:
        sql = f'SELECT COUNT(*) AS n FROM "{name}"'
        ensure_read_only(csv, sql)
        assert csv.execute_query(sql)["n"][0] >= 1
    assert csv.execute_query("SELECT amount FROM q1_sales_2026")["amount"][0] == 5
    assert csv.execute_query('SELECT amount FROM "Q1_SALES_2026"')["amount"][0] == 5

    folder = _folder(tmp_path / "Reports 2026-Q1")
    pd.DataFrame({"n": [1, 2]}).to_parquet(folder / "Net Sales (final).parquet")
    duck = DuckDBClient(uris=str(folder / "Net Sales (final).parquet"))
    (view,) = [t.name for t in duck.get_tables()]
    assert duck.execute_query(f'SELECT SUM(n) AS n FROM "{view}"')["n"][0] == 3
    assert duck.execute_query(f"SELECT SUM(n) AS n FROM {view}")["n"][0] == 3


def test_checks_without_a_session_still_refuse_paths_but_not_quoted_qualifiers():
    """Connectors with no catalog of their own to ask (Iceberg, network-dir
    tables) keep the strict reading."""
    check_sql('SELECT id FROM "Sales Ops".orders', "duckdb")
    check_sql('SELECT id FROM "sales-ops"."Order Lines"', "duckdb")
    for sql in ('SELECT * FROM "/etc/hostname"', 'SELECT * FROM "export.csv"', "SELECT * FROM 'orders'",
                'SELECT * FROM "s3://b/x.parquet"', 'SELECT * FROM "Sales Ops"."x.csv"'):
        with pytest.raises(ReadOnlyViolation):
            check_sql(sql, "duckdb")
