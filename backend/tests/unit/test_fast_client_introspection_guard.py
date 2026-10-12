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
])
def test_path_disclosing_introspection_is_refused(sql):
    with pytest.raises(ValueError, match="introspection"):
        FastQueryClient([]).execute_query(sql)


@pytest.mark.parametrize("sql", [
    "SELECT 1 AS query",
    "SELECT 42 AS query_count",
    "SELECT table_name FROM duckdb_tables()",
    "SELECT * FROM (SELECT 'settings' AS database_name) t",
])
def test_ordinary_queries_still_run(sql):
    df = FastQueryClient([]).execute_query(sql)
    assert df is not None
