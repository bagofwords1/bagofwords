"""Untrusted text never reaches a DuckDB parse that can open files or
addresses.

Contract:
- DuckDB expands the import-database forms while it parses, so every place
  that hands agent text to DuckDB does so on an engine that is already
  locked (external access off, configuration locked), or after a check
  that refuses the text;
- the text checks refuse IMPORT DATABASE, EXPORT DATABASE and any PRAGMA
  outside the DuckDB connections' read list for the duckdb dialect;
- this holds for the network directory's file tables (with and without a
  table selection), the accelerated custom tables, the DuckDB statement
  inspector, DuckDB connections, the file connections (CSV, QVD, PBIX,
  Power BI Report Server) and Iceberg.

Each case watches a scratch directory for any file opened in it (inotify)
and a loopback listener, run as a separate process, for any request.
Linux only (inotify).
"""

from __future__ import annotations

import ctypes
import errno
import os
import struct
import subprocess
import sys
import textwrap
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from app.ai.code_execution.duckdb_guard import inspect_duckdb_sql
from app.ai.code_execution.sql_guard import ReadOnlyViolation, check_sql, ensure_read_only
from tests.unit.test_duckdb_file_lockdown import files  # noqa: F401  (the four file connections)
from tests.unit.test_lakehouse_clients import _ice, iceberg_catalog  # noqa: F401  (a real Iceberg catalog)

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify is Linux only")

_IN_ACCESS, _IN_OPEN, _IN_NONBLOCK = 0x1, 0x20, 0o4000


class _Opens:
    """The names of files opened or read in `directory` since the last call."""

    def __init__(self, directory: Path):
        self._libc = ctypes.CDLL("libc.so.6", use_errno=True)
        self._fd = self._libc.inotify_init1(_IN_NONBLOCK)
        assert self._fd >= 0, os.strerror(ctypes.get_errno())
        assert self._libc.inotify_add_watch(self._fd, os.fsencode(directory), _IN_OPEN | _IN_ACCESS) >= 0

    def __call__(self) -> set[str]:
        names = set()
        while True:
            try:
                buf = os.read(self._fd, 65536)
            except BlockingIOError:
                return names
            except OSError as err:
                if err.errno == errno.EAGAIN:
                    return names
                raise
            i = 0
            while i < len(buf):
                _wd, _mask, _cookie, length = struct.unpack_from("iIII", buf, i)
                names.add(buf[i + 16:i + 16 + length].rstrip(b"\0").decode())
                i += 16 + length

    def close(self):
        os.close(self._fd)


_LISTENER = textwrap.dedent("""
    import http.server, sys
    log = sys.argv[1]
    class H(http.server.BaseHTTPRequestHandler):
        def _answer(self, body):
            with open(log, "a") as f:
                f.write(self.command + " " + self.path + chr(10))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return body
        def do_GET(self):
            self.wfile.write(self._answer(b"SELECT 1;"))
        def do_HEAD(self):
            self._answer(b"SELECT 1;")
        def log_message(self, *a):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), H)
    print(server.server_address[1], flush=True)
    server.serve_forever()
""")


@pytest.fixture
def watched(tmp_path):
    """`target`: a directory holding an exported database's two files;
    `url`: a loopback listener; `access()`: the files opened in `target`
    and the requests the listener received since the last call."""
    target = tmp_path / "exported"
    target.mkdir()
    (target / "schema.sql").write_text("CREATE TABLE sentinel (a INTEGER);\n")
    (target / "load.sql").write_text("INSERT INTO sentinel VALUES (1);\n")
    log = tmp_path / "requests.log"
    proc = subprocess.Popen([sys.executable, "-I", "-c", _LISTENER, str(log)], stdout=subprocess.PIPE, text=True)
    opens = _Opens(target)
    try:
        url = f"http://127.0.0.1:{int(proc.stdout.readline())}"

        def requests() -> list[str]:
            lines = log.read_text().splitlines() if log.exists() else []
            log.unlink(missing_ok=True)
            return lines

        def access() -> tuple[set[str], list[str]]:
            return opens(), requests()

        # Both watches see what they are meant to see.
        (target / "schema.sql").read_text()
        urllib.request.urlopen(f"{url}/control", timeout=5).read()
        assert access() == ({"schema.sql"}, ["GET /control"])
        yield SimpleNamespace(target=target, url=url, access=access)
    finally:
        opens.close()
        proc.kill()
        proc.wait()


_FORMS = [
    "IMPORT DATABASE '{local}'",
    "PRAGMA import_database('{local}')",
    "/* a note */ import database '{local}';",
    "pragma IMPORT_DATABASE ('{local}')",
    "; PRAGMA import_database('{local}')",
    "IMPORT DATABASE '{url}/one'",
    "PRAGMA import_database('{url}/two')",
    "EXPORT DATABASE '{local}/out'",
    # A leading space outside ASCII that DuckDB reads as whitespace.
    chr(0xA0) + "PRAGMA import_database('{local}')",
    chr(0x2000) + "PRAGMA import_database('{url}/three')",
    chr(0x3000) + "IMPORT DATABASE '{local}'",
]

# The code points outside ASCII DuckDB 1.5.4 reads as whitespace.
_DUCKDB_UNICODE_SPACES = [0xA0, *range(0x2000, 0x200C), 0x202F, 0x205F, 0x2060, 0x3000, 0xFEFF]


def _form(template: str, watched) -> str:
    return template.format(local=str(watched.target).replace("'", "''"), url=watched.url)


def _nothing_reached(watched):
    assert watched.access() == (set(), [])
    assert not (watched.target / "out").exists()


# ------------------------------------------------------------ the text checks

@pytest.mark.parametrize("template", _FORMS)
def test_the_duckdb_text_check_refuses_the_import_and_export_forms(watched, template):
    with pytest.raises(ReadOnlyViolation):
        check_sql(_form(template, watched), "duckdb")
    _nothing_reached(watched)


@pytest.mark.parametrize("sql", [
    "PRAGMA version", "PRAGMA show_tables", "PRAGMA table_info('indexed_files')", "SELECT 1 AS n",
])
def test_the_duckdb_text_check_keeps_the_read_pragmas(sql):
    check_sql(sql, "duckdb")


@pytest.mark.parametrize("code_point", _DUCKDB_UNICODE_SPACES, ids=lambda c: f"U+{c:04X}")
def test_the_duckdb_text_check_reads_each_unicode_space_as_duckdb_does(code_point):
    assert len(_DUCKDB_UNICODE_SPACES) == 18
    with pytest.raises(ReadOnlyViolation):
        check_sql(chr(code_point) + "PRAGMA import_database('/nonexistent/db')", "duckdb")
    with pytest.raises(ReadOnlyViolation):
        check_sql("SELECT 1;" + chr(code_point) + "PRAGMA import_database('/nonexistent/db')", "duckdb")


def test_a_read_pragma_after_a_unicode_space_is_still_a_read(tmp_path):
    from app.ai.code_execution.sql_guard import DuckdbRead, duckdb_read
    from app.data_sources.clients.duckdb_client import DuckDBClient

    sql = chr(0xA0) + "PRAGMA show_tables"
    assert duckdb_read(sql) == DuckdbRead("PRAGMA")
    check_sql(sql, "duckdb")
    (tmp_path / "sales.csv").write_text("id,amount\n1,10\n")
    ensure_read_only(DuckDBClient(uris=str(tmp_path / "sales.csv")), sql)


@pytest.mark.parametrize("sql", ["@x", "1 AS n", chr(0x85) + "PRAGMA show_tables"])
def test_text_the_reads_list_cannot_read_at_its_start_is_refused(sql):
    from app.ai.code_execution.sql_guard import duckdb_read

    with pytest.raises(ReadOnlyViolation):
        duckdb_read(sql)


@pytest.mark.parametrize("template", _FORMS)
def test_the_statement_inspector_refuses_them_on_its_locked_engine(watched, template):
    with pytest.raises(ReadOnlyViolation):
        inspect_duckdb_sql(_form(template, watched))
    _nothing_reached(watched)


def test_the_statement_inspector_reads_back_a_locked_engine():
    from app.ai.code_execution.duckdb_guard import _LOCK, _connection

    with _LOCK:
        settings = _connection().execute(
            "SELECT current_setting('enable_external_access'), current_setting('lock_configuration')").fetchone()
    assert settings == (False, True)


# ------------------------------------------------------------ the network directory's file tables

def _network_dir(tmp_path, monkeypatch, *, selected: bool):
    from app.data_sources.clients._network_dir_tables import attach_catalog
    from app.data_sources.clients.network_dir_client import NetworkDirClient
    from app.services import storage

    monkeypatch.setattr(storage, "UPLOADS_ROOT", tmp_path / "cache")
    root = tmp_path / "share"
    root.mkdir()
    (root / "notes.txt").write_text("incident timeline")
    client = NetworkDirClient(root_path=str(root), index_mode="content")
    assert client.index_file_tables
    if selected:
        attach_catalog(client, [("indexed_files", [], {})], [])
    return client


@pytest.mark.parametrize("selected", [True, False], ids=["selected", "nothing-selected"])
@pytest.mark.parametrize("template", _FORMS)
def test_the_network_directory_refuses_them_before_duckdb_can_reach_anything(
        watched, tmp_path, monkeypatch, template, selected):
    client = _network_dir(tmp_path, monkeypatch, selected=selected)
    with pytest.raises(ReadOnlyViolation if selected else PermissionError):
        client.execute_query(_form(template, watched))
    _nothing_reached(watched)
    # The file-name form and a checked SELECT still work.
    assert client.execute_query("notes.txt") == "incident timeline"
    if selected:
        assert client.execute_query("SELECT count(*) AS n FROM indexed_files")["n"].tolist() == [0]


@pytest.mark.parametrize("template", _FORMS)
def test_the_network_directory_parses_only_on_a_locked_engine(watched, tmp_path, monkeypatch, template):
    """With the text check out of the way, the engine is locked before it
    reads the statement."""
    from app.ai.code_execution import sql_guard
    from app.data_sources.clients._network_dir_tables import query_tables

    client = _network_dir(tmp_path, monkeypatch, selected=True)
    monkeypatch.setattr(sql_guard, "check_sql", lambda sql, dialect: None)
    with pytest.raises((duckdb.Error, ValueError)):
        query_tables(client, _form(template, watched))
    _nothing_reached(watched)


# ------------------------------------------------------------ the other DuckDB connections

@pytest.mark.parametrize("template", _FORMS)
def test_the_accelerated_client_refuses_them_on_its_locked_engine(watched, template):
    from app.data_sources.fast.fast_client import FastQueryClient

    with pytest.raises((duckdb.Error, ValueError)):
        FastQueryClient([]).execute_query(_form(template, watched))
    _nothing_reached(watched)


@pytest.mark.parametrize("template", _FORMS)
def test_a_duckdb_connection_refuses_them_in_its_check_and_on_its_locked_engine(watched, tmp_path, template):
    from app.data_sources.clients.duckdb_client import DuckDBClient

    data = tmp_path / "data"
    data.mkdir()
    (data / "sales.csv").write_text("id,amount\n1,10\n")
    client = DuckDBClient(uris=str(data / "sales.csv"))
    sql = _form(template, watched)
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(client, sql)
    _nothing_reached(watched)
    with pytest.raises(Exception, match="(?i)permission|disabled|not allowed|catalog"):
        client.execute_query(sql)
    _nothing_reached(watched)


@pytest.mark.parametrize("template", _FORMS)
def test_each_file_connection_refuses_them(watched, files, template):  # noqa: F811
    _kind, _client, run = files
    with pytest.raises(ReadOnlyViolation):
        run(_form(template, watched))
    _nothing_reached(watched)


@pytest.mark.parametrize("query", [
    "SELECT excel_text(amount, '0.0') AS t FROM sales.orders",
    "SELECT CAST('10.0.0.1' AS INET) AS a, region FROM sales.orders",
    "SELECT stem(region, 'english') AS s FROM sales.orders",
])
def test_iceberg_runs_the_query_on_a_locked_engine(watched, iceberg_catalog, tmp_path, monkeypatch, query):  # noqa: F811
    """A function or type from an extension not loaded is not installed: the
    engine that runs the query has external access off. The engine here may
    install extensions from the loopback listener into an empty directory."""
    connect = duckdb.connect

    def connect_installing(database=":memory:", *args, config=None, **kwargs):
        config = {**(config or {}), "extension_directory": str(tmp_path / "extensions"),
                  "custom_extension_repository": watched.url,
                  "autoinstall_known_extensions": True, "autoload_known_extensions": True}
        return connect(database, *args, config=config, **kwargs)

    monkeypatch.setattr(duckdb, "connect", connect_installing)
    client = _ice(iceberg_catalog)
    with pytest.raises(duckdb.Error):
        client.execute_query(query)
    _nothing_reached(watched)
    assert not (tmp_path / "extensions").exists() or not any((tmp_path / "extensions").rglob("*.duckdb_extension"))
    df = client.execute_query("SELECT region, COUNT(*) AS n FROM sales.orders GROUP BY region ORDER BY region")
    assert dict(zip(df.region, df.n, strict=True)) == {"AMER": 30, "APAC": 30, "EMEA": 30}


@pytest.mark.parametrize("template", _FORMS)
def test_iceberg_refuses_them_before_any_engine(watched, template):
    from app.data_sources.clients.iceberg_client import IcebergClient

    client = object.__new__(IcebergClient)
    sql = _form(template, watched)
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(client, sql)
    with pytest.raises(ReadOnlyViolation):
        client.execute_query(sql)
    _nothing_reached(watched)
