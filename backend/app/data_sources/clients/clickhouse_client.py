from app.data_sources.clients.base import DataSourceClient

import pandas as pd
import clickhouse_connect
from typing import List, Generator
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from contextlib import contextmanager


class ClickhouseClient(DataSourceClient):
    def __init__(self, host, port, user, password, database, secure=True):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        # Accept comma-separated databases; use the first as the default
        self.database = database
        self._databases = []
        if isinstance(self.database, str) and self.database.strip():
            parts = [d.strip() for d in self.database.split(",") if d.strip()]
            seen = set()
            for d in parts:
                if d not in seen:
                    seen.add(d)
                    self._databases.append(d)
        # Primary database for the connection/session
        self._primary_database = self._databases[0] if self._databases else self.database
        self.secure = secure

        client_kwargs = {
            "host": self.host,
            "port": self.port,
            "username": self.user,
            "password": self.password,
            "secure": self.secure,
            "verify": not self.secure,
        }
        # Only include database if provided; otherwise let server default apply
        if self._primary_database:
            client_kwargs["database"] = self._primary_database
        self.client = clickhouse_connect.get_client(**client_kwargs)

    @contextmanager
    def connect(self) -> Generator[clickhouse_connect.driver.Client, None, None]:
        """Yield a connection to ClickHouse."""
        try:
            yield self.client
        finally:
            # No specific close method for clickhouse_connect, but ensuring resource cleanup if needed
            pass

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Run SQL statement and return the result as a DataFrame."""
        try:
            with self.connect() as conn:
                result = conn.query(sql)
                df = pd.DataFrame(result.result_set, columns=result.column_names)
                return df
        except Exception as e:
            print(f"Error executing SQL: {e}")
            raise

    def get_tables(self) -> List[Table]:
        """Get tables with graceful fallback if enriched query fails."""
        try:
            return self._get_tables_enriched()
        except Exception:
            return self._get_tables_basic()

    def _get_tables_enriched(self) -> List[Table]:
        """Get tables with column/table comments. May fail on some ClickHouse versions."""
        with self.connect() as conn:
            if self._databases:
                quoted = ", ".join([f"'{d.replace("'", "''")}'" for d in self._databases])
                where_sql = f"WHERE c.database IN ({quoted})"
            elif self._primary_database:
                where_sql = f"WHERE c.database = '{self._primary_database.replace("'", "''")}'"
            else:
                where_sql = ""

            sql = f"""
                SELECT
                    c.database,
                    c.table AS table_name,
                    c.name AS column_name,
                    c.type AS data_type,
                    c.comment AS column_comment,
                    t.comment AS table_comment
                FROM system.columns c
                LEFT JOIN system.tables t
                    ON c.database = t.database AND c.table = t.name
                {where_sql}
                ORDER BY c.database, c.table, c.position
            """

            result = conn.query(sql).result_rows

            tables = {}
            for row in result:
                database_name, table_name, column_name, data_type, col_comment, tbl_comment = row
                fqn = f"{database_name}.{table_name}"

                if fqn not in tables:
                    tables[fqn] = Table(
                        name=fqn,
                        description=tbl_comment if tbl_comment else None,
                        columns=[],
                        pks=None,
                        fks=None,
                        metadata_json={"database": database_name}
                    )
                tables[fqn].columns.append(TableColumn(
                    name=column_name,
                    dtype=data_type,
                    description=col_comment if col_comment else None
                ))

            return list(tables.values())

    def _get_tables_basic(self) -> List[Table]:
        """Get tables without comments (original query - always works)."""
        try:
            with self.connect() as conn:
                if self._databases:
                    quoted = ", ".join([f"'{d.replace("'", "''")}'" for d in self._databases])
                    where_sql = f"WHERE database IN ({quoted})"
                elif self._primary_database:
                    where_sql = f"WHERE database = '{self._primary_database.replace("'", "''")}'"
                else:
                    where_sql = ""

                sql = f"""
                    SELECT
                        database,
                        table AS table_name,
                        name AS column_name,
                        type AS data_type
                    FROM system.columns
                    {where_sql}
                    ORDER BY database, table_name, position
                """

                result = conn.query(sql).result_rows

                tables = {}
                for row in result:
                    database_name, table_name, column_name, data_type = row
                    fqn = f"{database_name}.{table_name}"

                    if fqn not in tables:
                        tables[fqn] = Table(
                            name=fqn, columns=[], pks=None, fks=None)
                    tables[fqn].columns.append(
                        TableColumn(name=column_name, dtype=data_type))

                return list(tables.values())
        except Exception as e:
            print(f"Error retrieving tables: {e}")
            return []

    def get_schema(self, table: str) -> Table:
        """This method is now obsolete. Please use get_tables() instead."""
        raise NotImplementedError(
            "get_schema() is obsolete. Use get_tables() instead.")

    def get_schemas(self):
        """Get schemas for all tables in the specified database."""
        return self.get_tables()

    def prompt_schema(self):
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        """Test connection to ClickHouse and return status information."""
        try:
            with self.connect() as conn:
                conn.query("SELECT 1")
                return {
                    "success": True,
                    "message": "Successfully connected to ClickHouse"
                }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @property
    def description(self):
        if self._databases:
            databases = ", ".join(self._databases)
            description = f"ClickHouse database(s) '{databases}' at {self.host}"
        elif self._primary_database:
            description = f"ClickHouse database '{self._primary_database}' at {self.host}"
        else:
            description = f"ClickHouse (all accessible databases) at {self.host}"
        return description
