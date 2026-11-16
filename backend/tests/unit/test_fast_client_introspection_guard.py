"""FastQueryClient refuses DuckDB introspection that discloses server paths (pentest 829413666)."""
import pytest

from app.data_sources.fast.fast_client import FastQueryClient


@pytest.mark.parametrize("sql", [
    "SELECT path FROM duckdb_databases()",
    "select * from DUCKDB_DATABASES",
    "PRAGMA database_list",
    "SELECT file FROM pragma_database_list",
    "SELECT value FROM duckdb_settings() WHERE name = 'temp_directory'",
    "SELECT current_setting('home_directory')",
    "SELECT * FROM duckdb_extensions()",
    "SELECT * FROM query_table('duck' || 'db_databases')",
    "SELECT * FROM query ('SELECT 1')",
    "SELECT name, setting FROM pg_settings",
    "select * from PG_CATALOG.PG_SETTINGS",
])
def test_path_disclosing_introspection_is_refused(sql):
    with pytest.raises(ValueError, match="introspection"):
        FastQueryClient([]).execute_query(sql)


@pytest.mark.parametrize("sql", [
    "SELECT 1 AS query",
    "SELECT 42 AS query_count",
    "SELECT table_name FROM duckdb_tables()",
    "SELECT * FROM (SELECT 'settings' AS database_name) t",
    "SELECT column_name FROM duckdb_columns()",
    "DESCRIBE SELECT 1 AS n",
    "SELECT 1 AS pg_settings_count",
    "FROM (SELECT 1 AS n)",
    "SHOW TABLES",
    "WITH a AS (SELECT 1 AS n) SELECT n FROM a;",
])
def test_ordinary_queries_still_run(sql):
    df = FastQueryClient([]).execute_query(sql)
    assert df is not None


def test_a_single_select_returns_its_rows():
    assert FastQueryClient([]).execute_query("SELECT 2 AS n UNION ALL SELECT 3")["n"].tolist() == [2, 3]


@pytest.mark.parametrize("sql", [
    "SELECT 1 AS a; SELECT 2 AS b",
    "CALL pragma_version()",
    "EXPLAIN SELECT 1",
])
def test_anything_but_one_select_is_refused(sql):
    with pytest.raises(ValueError, match="one read-only SELECT"):
        FastQueryClient([]).execute_query(sql)


_TWO_STATEMENTS_AFTER_FILE_LOGGING = """
import sys
from app.data_sources.fast.fast_client import FastQueryClient
sql = "CALL enable_logging(storage='file', storage_path='" + sys.argv[1].replace("'", "''") + "'); SELECT 1"
try:
    FastQueryClient([]).execute_query(sql)
except ValueError as e:
    print("refused:", e)
else:
    print("ran")
"""


def test_a_second_statement_after_file_logging_is_refused_without_ending_the_process(tmp_path):
    """Run in its own process: DuckDB 1.5.4 aborts the process on this text
    when it executes it."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    done = subprocess.run(
        [sys.executable, "-c", _TWO_STATEMENTS_AFTER_FILE_LOGGING, str(tmp_path / "logs")],
        cwd=Path(__file__).resolve().parents[2], env=os.environ.copy(), capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, (done.returncode, done.stderr[-2000:])
    assert done.stdout.startswith("refused:"), done.stdout
    assert not (tmp_path / "logs").exists()
