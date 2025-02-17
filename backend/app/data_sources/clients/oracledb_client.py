from app.data_sources.clients.base import DataSourceClient

import pandas as pd
import sqlalchemy
from sqlalchemy import text
from contextlib import contextmanager
from typing import Generator, List
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property


class OracledbClient(DataSourceClient):
    def __init__(self, host, port, service_name, user, password):
        self.host = host
        self.port = port
        self.service_name = service_name
        self.user = user
        self.password = password

    @cached_property
    def oracle_uri(self):
        uri = (
            f"oracle+cx_oracle://{self.user}:{self.password}@"
            f"{self.host}:{self.port}/?service_name={self.service_name}"
        )
        return uri

    @contextmanager
    def connect(self) -> Generator[sqlalchemy.engine.base.Connection, None, None]:
        """Yield a connection to an Oracle database."""
        engine = None
        conn = None
        try:
            engine = sqlalchemy.create_engine(self.oracle_uri)
            conn = engine.connect()
            yield conn
        except Exception as e:
            raise RuntimeError(f"{e}")
        finally:
            if conn is not None:
                conn.close()
            if engine is not None:
                engine.dispose()

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute SQL statement and return the result as a DataFrame."""
        try:
            with self.connect() as conn:
                df = pd.read_sql(text(sql), conn)
            return df
        except Exception as e:
            print(f"Error executing SQL: {e}")
            raise

    def get_tables(self) -> List[Table]:
        """Get all tables and their columns in the specified database."""
        try:
            with self.connect() as conn:
                sql = """
                    SELECT table_name, column_name, data_type
                    FROM all_tab_columns
                    WHERE owner = :user
                    ORDER BY table_name, column_id
                """
                result = conn.execute(
                    text(sql), {'user': self.user.upper()}).fetchall()

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

    def get_schema(self, table_id: str) -> Table:
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
        """Test connection to Oracle and return status information."""
        try:
            with self.connect() as conn:
                conn.execute(text("SELECT 1 FROM DUAL"))
                return {
                    "success": True,
                    "message": "Successfully connected to Oracle"
                }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @property
    def description(self):
        description = f"Oracle client for service '{self.service_name}' at {self.host}:{self.port}"
        return description
