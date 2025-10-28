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
        self.database = database
        self.secure = secure

        self.client = clickhouse_connect.get_client(host=self.host, port=self.port, username=self.user, password=self.password, database=self.database, secure=self.secure, verify=not self.secure)

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
        """Get all tables and their columns in the specified database."""
        try:
            with self.connect() as conn:
                rows = conn.query("SELECT currentDatabase()").result_rows

                sql = f"""
                    SELECT
                        table AS table_name,
                        name AS column_name,
                        type AS data_type
                    FROM system.columns
                    WHERE database = '{self.database}'
                    ORDER BY table_name, position
                """

                result = conn.query(sql).result_rows

                tables = {}
                for row in result:
                    table_name, column_name, data_type = row

                    if table_name not in tables:
                        tables[table_name] = Table(
                            name=table_name, columns=[], pks=None, fks=None)
                    tables[table_name].columns.append(
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
        description = f"ClickHouse database '{self.database}' at {self.host}"
        return description
