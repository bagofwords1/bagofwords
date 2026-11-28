"""FastQueryClient refuses DuckDB introspection that discloses server paths (pentest 829413666)."""
import pytest

from app.data_sources.fast.fast_client import FastQueryClient
from tests.unit.test_client_classification import custom_table  # noqa: F401  (a real encrypted artifact)


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


# The blocked calls, with arguments, as a scalar and as a table function.
_BLOCKED_CALLS = [
    ("current_setting", "('temp_directory')"),
    ("which_secret", "('s3://bucket/x', 's3')"),
    ("getenv", "('HOME')"),
    ("query_table", "('notes')"),
    ("query", "('SELECT 1')"),
]
# How a name can be written so that a pattern over the text misses it while
# DuckDB still reads the call: quoted, split from its `(` by a space DuckDB
# reads as whitespace, or by a comment.
_SPELLINGS = [
    '"{name}"{args}',
    *("{name}" + z + "{args}" for z in (chr(0x200B), chr(0x2060), chr(0xFEFF))),
    "{name}/* note */{args}",
    "{name} -- note\n{args}",
]


@pytest.mark.parametrize("position", ["SELECT {call} AS x", "SELECT * FROM {call}"], ids=["scalar", "table"])
@pytest.mark.parametrize("spelling", _SPELLINGS, ids=repr)
@pytest.mark.parametrize("name,args", _BLOCKED_CALLS, ids=[n for n, _ in _BLOCKED_CALLS])
def test_a_blocked_call_is_refused_however_its_name_is_written(name, args, spelling, position):
    sql = position.format(call=spelling.format(name=name, args=args))
    with pytest.raises(ValueError, match="introspection"):
        FastQueryClient([]).execute_query(sql)


@pytest.mark.parametrize("sql", [
    "SELECT content FROM read_text('x.txt')",
    "SELECT * FROM read_blob('x')",
    "SELECT * FROM read_csv('x.csv')",
    "SELECT * FROM read_parquet('x.parquet')",
    "SELECT * FROM read_json_auto('x.json')",
    "SELECT * FROM glob('*')",
    "SELECT * FROM sniff_csv('x.csv')",
    "SELECT * FROM parquet_metadata('x.parquet')",
    "SELECT * FROM parquet_schema('x.parquet')",
    "SELECT * FROM parquet_kv_metadata('x.parquet')",
    "SELECT * FROM enable_logging()",
    "SELECT write_log('x') AS w",
    "SELECT * FROM duckdb_logs",
    "SELECT * FROM duckdb_temporary_files()",
    'SELECT * FROM "duckdb_settings"()',
    'SELECT * FROM "duckdb_databases"',
])
def test_file_reader_logging_and_server_state_functions_are_refused(sql):
    with pytest.raises(ValueError, match="introspection"):
        FastQueryClient([]).execute_query(sql)


def test_the_hidden_columns_are_not_reachable_through_a_quoted_query_table(custom_table):  # noqa: F811
    with pytest.raises(ValueError, match="introspection"):
        custom_table.execute_query("SELECT * FROM \"query_table\"('notes')")
    assert list(custom_table.execute_query("SELECT * FROM notes").columns) == ["id", "v"]


@pytest.mark.parametrize("sql,column,expected", [
    ("SELECT id FROM notes ORDER BY id", "id", [1, 2]),
    ("FROM notes SELECT id ORDER BY id", "id", [1, 2]),
    ("PRAGMA table_info('notes')", "name", ["id", "v"]),
    ("DESCRIBE notes", "column_name", ["id", "v"]),
    ("SHOW notes", "column_name", ["id", "v"]),
    ("DESCRIBE SELECT id, v FROM notes", "column_name", ["id", "v"]),
    ("SELECT column_name FROM duckdb_columns() WHERE table_name = 'notes' AND database_name = 'memory' "
     "ORDER BY column_index", "column_name", ["id", "v"]),
    ("SELECT column_name FROM information_schema.columns WHERE table_name = 'notes' "
     "AND table_catalog = 'memory' ORDER BY ordinal_position", "column_name", ["id", "v"]),
    ("SELECT name FROM pragma_table_info('notes')", "name", ["id", "v"]),
    ("SELECT * FROM notes PIVOT (count(*) FOR v IN ('a', 'b')) ORDER BY id", "id", [1, 2]),
    ("PIVOT notes ON v IN ('a', 'b') USING count(*) GROUP BY id ORDER BY id", "id", [1, 2]),
    ("WITH n AS (SELECT id FROM notes) SELECT id FROM n ORDER BY id", "id", [1, 2]),
])
def test_the_accelerated_reads_still_run(custom_table, sql, column, expected):  # noqa: F811
    assert custom_table.execute_query(sql)[column].tolist() == expected


def test_a_dynamic_pivot_is_refused_with_the_forms_that_work():
    with pytest.raises(ValueError, match=r"PIVOT needs an IN list.*SELECT \.\.\. PIVOT \(\.\.\.\)"):
        FastQueryClient([]).execute_query("PIVOT (SELECT 'a' AS v, 1 AS x) ON v USING sum(x)")
