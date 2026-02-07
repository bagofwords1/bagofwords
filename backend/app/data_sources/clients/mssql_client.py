from app.data_sources.clients.base import DataSourceClient

import pandas as pd
import sqlalchemy
from sqlalchemy import text
from contextlib import contextmanager
from typing import Generator, List
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property


class MSSQLClient(DataSourceClient):
    def __init__(self, host, port, database, user, password):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password

    @cached_property
    def sql_server_uri(self):
        uri = (
            f"mssql+pyodbc://{self.user}:{self.password}@"
            f"{self.host}:{self.port}/{self.database}?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
        )
        return uri

    @contextmanager
    def connect(self) -> Generator[sqlalchemy.engine.base.Connection, None, None]:
        """Yield a connection to a SQL Server database."""
        engine = None
        conn = None
        try:
            engine = sqlalchemy.create_engine(self.sql_server_uri)
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
        """Get tables with graceful fallback if enriched query fails."""
        try:
            return self._get_tables_enriched()
        except Exception:
            return self._get_tables_basic()

    def _get_tables_enriched(self) -> List[Table]:
        """Get tables with column/table descriptions via extended properties. May fail on some configurations."""
        with self.connect() as conn:
            sql = """
                SELECT
                    c.table_name,
                    c.column_name,
                    c.data_type,
                    CAST(ep_col.value AS NVARCHAR(MAX)) AS column_comment,
                    CAST(ep_tbl.value AS NVARCHAR(MAX)) AS table_comment
                FROM information_schema.columns c
                LEFT JOIN sys.columns sc
                    ON sc.name = c.column_name
                    AND sc.object_id = OBJECT_ID(c.table_schema + '.' + c.table_name)
                LEFT JOIN sys.extended_properties ep_col
                    ON ep_col.major_id = sc.object_id
                    AND ep_col.minor_id = sc.column_id
                    AND ep_col.name = 'MS_Description'
                LEFT JOIN sys.extended_properties ep_tbl
                    ON ep_tbl.major_id = OBJECT_ID(c.table_schema + '.' + c.table_name)
                    AND ep_tbl.minor_id = 0
                    AND ep_tbl.name = 'MS_Description'
                WHERE c.table_catalog = :database
                ORDER BY c.table_name, c.ordinal_position
            """
            result = conn.execute(text(sql), {'database': self.database}).fetchall()

            tables = {}
            for row in result:
                table_name, column_name, data_type, col_comment, tbl_comment = row

                if table_name not in tables:
                    tables[table_name] = Table(
                        name=table_name,
                        description=tbl_comment if tbl_comment else None,
                        columns=[],
                        pks=None,
                        fks=None
                    )
                tables[table_name].columns.append(TableColumn(
                    name=column_name,
                    dtype=data_type,
                    description=col_comment if col_comment else None
                ))
            return list(tables.values())

    def _get_tables_basic(self) -> List[Table]:
        """Get tables without comments (original query - always works)."""
        try:
            with self.connect() as conn:
                sql = """
                    SELECT table_name, column_name, data_type
                    FROM information_schema.columns
                    WHERE table_catalog = :database
                    ORDER BY table_name, ordinal_position
                """
                result = conn.execute(
                    text(sql), {'database': self.database}).fetchall()

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
        """Test connection to SQL Server and return status information."""
        try:
            with self.connect() as conn:
                conn.execute(text("SELECT 1"))
                return {
                    "success": True,
                    "message": "Successfully connected to SQL Server"
                }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @property
    def description(self):
        description = f"SQL Server client for database '{self.database}' at {self.host}:{self.port}"
        return description
