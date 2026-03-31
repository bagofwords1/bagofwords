from app.data_sources.clients.base import DataSourceClient

import pandas as pd
import sqlalchemy
from sqlalchemy import text
from contextlib import contextmanager
from typing import Generator, List, Optional
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property


class MSSQLClient(DataSourceClient):
    SUPPORTED_ODBC_DRIVERS = {17, 18}

    def __init__(self, host, port, database, user, password, schema: Optional[str] = None,
                 odbc_driver: int = 18, encrypt: bool = True):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.schema = schema
        self.odbc_driver = int(odbc_driver)
        if self.odbc_driver not in self.SUPPORTED_ODBC_DRIVERS:
            raise ValueError(f"Unsupported ODBC driver version: {self.odbc_driver}. Supported: {sorted(self.SUPPORTED_ODBC_DRIVERS)}")
        self.encrypt = encrypt
        self._schemas = []
        if isinstance(self.schema, str) and self.schema.strip():
            parts = [s.strip() for s in self.schema.split(",") if s.strip()]
            seen = set()
            for p in parts:
                if p not in seen:
                    seen.add(p)
                    self._schemas.append(p)

    @cached_property
    def sql_server_uri(self):
        from urllib.parse import quote_plus
        driver_name = f"ODBC Driver {self.odbc_driver} for SQL Server"
        params = (
            f"DRIVER={driver_name};"
            f"SERVER={self.host},{self.port};"
            f"DATABASE={self.database};"
            f"UID={self.user};"
            f"PWD={self.password};"
            f"TrustServerCertificate=yes;"
            f"LoginTimeout=30;"
        )
        if not self.encrypt:
            params += "Encrypt=no;"
        return f"mssql+pyodbc:///?odbc_connect={quote_plus(params)}"

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
            params = {"database": self.database}
            where_clauses = ["c.table_catalog = :database"]

            if self._schemas:
                in_keys = []
                for idx, sch in enumerate(self._schemas):
                    key = f"s{idx}"
                    params[key] = sch
                    in_keys.append(f":{key}")
                where_clauses.append(f"c.table_schema IN ({', '.join(in_keys)})")

            where_sql = " AND ".join(where_clauses)
            sql = text(f"""
                SELECT
                    c.table_schema,
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
                WHERE {where_sql}
                ORDER BY c.table_schema, c.table_name, c.ordinal_position
            """)
            result = conn.execute(sql, params).fetchall()

            tables = {}
            for row in result:
                table_schema, table_name, column_name, data_type, col_comment, tbl_comment = row
                key = (table_schema, table_name)
                fqn = f"{table_schema}.{table_name}"

                if key not in tables:
                    tables[key] = Table(
                        name=fqn,
                        description=tbl_comment if tbl_comment else None,
                        columns=[],
                        pks=None,
                        fks=None,
                        metadata_json={"schema": table_schema}
                    )
                tables[key].columns.append(TableColumn(
                    name=column_name,
                    dtype=data_type,
                    description=col_comment if col_comment else None
                ))
            return list(tables.values())

    def _get_tables_basic(self) -> List[Table]:
        """Get tables without comments (original query - always works)."""
        try:
            with self.connect() as conn:
                params = {"database": self.database}
                where_clauses = ["table_catalog = :database"]

                if self._schemas:
                    in_keys = []
                    for idx, sch in enumerate(self._schemas):
                        key = f"s{idx}"
                        params[key] = sch
                        in_keys.append(f":{key}")
                    where_clauses.append(f"table_schema IN ({', '.join(in_keys)})")

                where_sql = " AND ".join(where_clauses)
                sql = text(f"""
                    SELECT table_schema, table_name, column_name, data_type
                    FROM information_schema.columns
                    WHERE {where_sql}
                    ORDER BY table_schema, table_name, ordinal_position
                """)
                result = conn.execute(sql, params).fetchall()

                tables = {}
                for row in result:
                    table_schema, table_name, column_name, data_type = row
                    key = (table_schema, table_name)
                    fqn = f"{table_schema}.{table_name}"

                    if key not in tables:
                        tables[key] = Table(
                            name=fqn, columns=[], pks=None, fks=None,
                            metadata_json={"schema": table_schema})
                    tables[key].columns.append(
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
