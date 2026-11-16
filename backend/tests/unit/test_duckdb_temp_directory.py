"""The DuckDB engines that run agent SQL reach no temp directory.

Contract:
- DuckDB allows a session its own temp directory even with external access
  off. By default that is `.tmp` under the working directory, shared by every
  in-memory session of the process, so one session could read what another
  spilled there;
- every lock helper (`lock_query_session`, used by accelerated data, the
  network directory's file tables and Iceberg, and `lock_down`, used by the
  file and DuckDB connections) turns the temp directory off before it locks,
  so a locked engine reaches no temp directory, its own or another
  session's. Spilling to disk is off on those engines;
- accelerated data and the network directory refuse a read of the temp
  directory, with the call written plainly or split by a space DuckDB reads
  as whitespace and the query checks did not.
"""

from __future__ import annotations

import duckdb
import pytest

from app.data_sources.clients._duckdb_lockdown import lock_down
from app.data_sources.fast.fast_client import FastQueryClient, lock_query_session
from tests.unit.test_duckdb_parse_time_access import _network_dir

_ZERO_WIDTH = [chr(0x200B), chr(0x2060), chr(0xFEFF)]
_READS = [
    "SELECT * FROM glob('.tmp/*')",
    "SELECT content FROM read_text('.tmp/*')",
    "SELECT count(*) AS n FROM read_blob('.tmp/**')",
]
_SPLIT_READS = [
    "SELECT * FROM glob{z}('.tmp/*')",
    "SELECT content FROM{z}read_text('.tmp/*')",
    "SELECT count(*) AS n FROM read_blob{z}('.tmp/**')",
]
_ALL_READS = _READS + [t.format(z=z) for t in _SPLIT_READS for z in _ZERO_WIDTH]


@pytest.fixture
def planted(tmp_path, monkeypatch):
    """A file in the engine's default temp directory (`.tmp` under the
    working directory, here `tmp_path/work`)."""
    work = tmp_path / "work"
    (work / ".tmp").mkdir(parents=True)
    (work / ".tmp" / "planted.txt").write_text("another session's spill")
    monkeypatch.chdir(work)
    return tmp_path


def _lock_down(con):
    lock_down(con)


@pytest.mark.parametrize("lock", [lock_query_session, _lock_down], ids=["lock_query_session", "lock_down"])
@pytest.mark.parametrize("sql", _READS)
def test_a_locked_engine_reaches_no_temp_directory(planted, lock, sql):
    with duckdb.connect() as con:
        lock(con)
        with pytest.raises(duckdb.PermissionException):
            con.execute(sql).fetchall()
        assert con.execute("SELECT current_setting('temp_directory')").fetchone() == ("",)


def test_another_sessions_spill_is_not_reachable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with duckdb.connect(config={"memory_limit": "50MB"}) as other:
        # Larger than its memory limit: the session spills into the shared
        # default temp directory.
        other.execute("CREATE TABLE big AS SELECT md5(i::VARCHAR) AS s FROM range(3000000) t(i)")
        spilled = [path for (path,) in other.execute("SELECT path FROM duckdb_temporary_files()").fetchall()]
        assert spilled
        with duckdb.connect() as con:
            lock_query_session(con)
            with pytest.raises(duckdb.PermissionException):
                con.execute("SELECT * FROM glob('.tmp/*')").fetchall()
            for path in spilled:
                literal = path.replace("'", "''")
                with pytest.raises(duckdb.PermissionException):
                    con.execute(f"SELECT count(*) FROM read_blob('{literal}')").fetchall()


@pytest.mark.parametrize("sql", _ALL_READS, ids=repr)
def test_accelerated_data_cannot_read_or_glob_the_temp_directory(planted, sql):
    with pytest.raises((ValueError, duckdb.Error)):
        FastQueryClient([]).execute_query(sql)


@pytest.mark.parametrize("sql", _ALL_READS, ids=repr)
def test_the_network_directory_cannot_read_or_glob_the_temp_directory(planted, monkeypatch, sql):
    client = _network_dir(planted, monkeypatch, selected=True)
    with pytest.raises((ValueError, duckdb.Error)):
        client.execute_query(sql)
    assert client.execute_query("SELECT count(*) AS n FROM indexed_files")["n"].tolist() == [0]


def test_a_large_sort_runs_in_memory_on_accelerated_data():
    """Spilling is off on a locked engine; a sort that fits in memory runs."""
    df = FastQueryClient([]).execute_query(
        "SELECT count(*) AS n, min(h) = (SELECT min(hash(i)) FROM range(2000000) t(i)) AS ordered "
        "FROM (SELECT hash(i) AS h FROM range(2000000) t(i) ORDER BY h)")
    assert df["n"].tolist() == [2000000]
    assert df["ordered"].tolist() == [True]
