from app.data_sources.clients.base import DataSourceClient

import pandas as pd
import struct
from contextlib import contextmanager
from typing import Generator, List, Optional
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter

import pyodbc
from azure.identity import ClientSecretCredential


class MsFabricClient(DataSourceClient):
    """Client for Microsoft Fabric Warehouse/Lakehouse SQL endpoints."""

    def __init__(
        self,
        server_hostname: str,
        database: str,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        schema: Optional[str] = None,
    ):
        self.server_hostname = server_hostname
        self.database = database
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.schema = schema

        # Parse comma-separated schemas if provided
        self._schemas: List[str] = []
        if isinstance(self.schema, str) and self.schema.strip():
            parts = [s.strip() for s in self.schema.split(",") if s.strip()]
            # Dedupe while preserving order
            seen = set()
            for p in parts:
                if p not in seen:
                    seen.add(p)
                    self._schemas.append(p)

    def _get_access_token(self) -> str:
        """Get Azure AD access token for SQL endpoint."""
        credential = ClientSecretCredential(
            tenant_id=self.tenant_id,
            client_id=self.client_id,
            client_secret=self.client_secret,
        )
        token = credential.get_token("https://database.windows.net/.default")
        return token.token

    def _get_token_struct(self) -> bytes:
        """Convert token to struct format required by pyodbc."""
        token = self._get_access_token()
        token_bytes = token.encode("utf-16-le")
        token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
        return token_struct

    @contextmanager
    def connect(self) -> Generator:
        """Yield a connection to Microsoft Fabric SQL endpoint."""
        conn = None
        try:
            # Build connection string for Fabric
            conn_str = (
                f"DRIVER={{ODBC Driver 18 for SQL Server}};"
                f"SERVER={self.server_hostname};"
                f"DATABASE={self.database};"
                f"Encrypt=yes;"
                f"TrustServerCertificate=no;"
            )

            # Get token and pass via attrs_before
            token_struct = self._get_token_struct()
            # SQL_COPT_SS_ACCESS_TOKEN = 1256
            conn = pyodbc.connect(conn_str, attrs_before={1256: token_struct})
            yield conn
        except Exception as e:
            raise RuntimeError(f"Error connecting to Microsoft Fabric: {e}")
        finally:
            if conn is not None:
                conn.close()

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute SQL statement and return the result as a DataFrame."""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute(sql)
                # Fetch column names from cursor description
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                rows = cursor.fetchall()
                cursor.close()
                df = pd.DataFrame.from_records(rows, columns=columns)
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
        """Get tables with column/table descriptions. May fail on some configurations."""
        tables = {}
        with self.connect() as conn:
            cursor = conn.cursor()

            where_clauses = [f"c.TABLE_CATALOG = '{self.database}'"]
            where_clauses.append("c.TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'queryinsights')")
            if self._schemas:
                schema_list = ", ".join([f"'{s}'" for s in self._schemas])
                where_clauses.append(f"c.TABLE_SCHEMA IN ({schema_list})")

            where_sql = " WHERE " + " AND ".join(where_clauses)

            # Fabric supports extended properties for descriptions
            sql = f"""
                SELECT
                    c.TABLE_SCHEMA,
                    c.TABLE_NAME,
                    c.COLUMN_NAME,
                    c.DATA_TYPE,
                    CAST(ep_col.value AS NVARCHAR(MAX)) AS column_comment,
                    CAST(ep_tbl.value AS NVARCHAR(MAX)) AS table_comment
                FROM INFORMATION_SCHEMA.COLUMNS c
                LEFT JOIN sys.columns sc
                    ON sc.name = c.COLUMN_NAME
                    AND sc.object_id = OBJECT_ID(c.TABLE_SCHEMA + '.' + c.TABLE_NAME)
                LEFT JOIN sys.extended_properties ep_col
                    ON ep_col.major_id = sc.object_id
                    AND ep_col.minor_id = sc.column_id
                    AND ep_col.name = 'MS_Description'
                LEFT JOIN sys.extended_properties ep_tbl
                    ON ep_tbl.major_id = OBJECT_ID(c.TABLE_SCHEMA + '.' + c.TABLE_NAME)
                    AND ep_tbl.minor_id = 0
                    AND ep_tbl.name = 'MS_Description'
                {where_sql}
                ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION
            """

            cursor.execute(sql)
            results = cursor.fetchall()
            cursor.close()

            for row in results:
                table_schema, table_name, column_name, data_type, col_comment, tbl_comment = row
                key = (table_schema, table_name)
                fqn = f"{table_schema}.{table_name}"
                if key not in tables:
                    tables[key] = Table(
                        name=fqn,
                        description=tbl_comment if tbl_comment else None,
                        columns=[],
                        pks=[],
                        fks=[],
                        metadata_json={"schema": table_schema, "database": self.database}
                    )
                tables[key].columns.append(TableColumn(
                    name=column_name,
                    dtype=data_type,
                    description=col_comment if col_comment else None
                ))

        return list(tables.values())

    def _get_tables_basic(self) -> List[Table]:
        """Get tables without comments (always works)."""
        tables = {}
        with self.connect() as conn:
            cursor = conn.cursor()

            where_clauses = [f"TABLE_CATALOG = '{self.database}'"]
            where_clauses.append("TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'queryinsights')")
            if self._schemas:
                schema_list = ", ".join([f"'{s}'" for s in self._schemas])
                where_clauses.append(f"TABLE_SCHEMA IN ({schema_list})")

            where_sql = " WHERE " + " AND ".join(where_clauses)

            sql = f"""
                SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS
                {where_sql}
                ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
            """

            cursor.execute(sql)
            results = cursor.fetchall()
            cursor.close()

            for row in results:
                table_schema, table_name, column_name, data_type = row
                key = (table_schema, table_name)
                fqn = f"{table_schema}.{table_name}"
                if key not in tables:
                    tables[key] = Table(
                        name=fqn,
                        columns=[],
                        pks=[],
                        fks=[],
                        metadata_json={"schema": table_schema, "database": self.database}
                    )
                tables[key].columns.append(TableColumn(name=column_name, dtype=data_type))

        return list(tables.values())

    def get_schema(self, table_name: str) -> Table:
        """Get schema for a specific table. Deprecated - use get_tables() instead."""
        raise NotImplementedError("get_schema() is deprecated. Use get_tables() instead.")

    def get_schemas(self) -> List[Table]:
        """Get all table schemas. Wrapper for get_tables()."""
        return self.get_tables()

    def prompt_schema(self) -> str:
        """Return formatted schema string for LLM prompts."""
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self) -> dict:
        """Test connection to Microsoft Fabric and return status information."""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                cursor.close()
                return {
                    "success": True,
                    "message": "Successfully connected to Microsoft Fabric"
                }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @property
    def description(self) -> str:
        """System prompt describing this data source for LLM context."""
        schema_info = ", ".join(self._schemas) if self._schemas else "all schemas"
        return f"""Microsoft Fabric SQL Endpoint
Server: {self.server_hostname}
Database: {self.database}
Schemas: {schema_info}

Microsoft Fabric uses T-SQL syntax (SQL Server compatible).
Tables are organized in a two-level namespace: schema.table

T-SQL syntax rules:
- Use TOP N instead of LIMIT (e.g., SELECT TOP 10 * FROM table)
- String concatenation uses + not ||
- Use GETDATE() instead of NOW()
- Use ISNULL() or COALESCE() for null handling
- Use DATEPART(), DATEADD(), DATEDIFF() for date operations
- When using UNION/INTERSECT/EXCEPT with ORDER BY, the ORDER BY column must appear in the SELECT list
- Use square brackets [column] for reserved words or special characters
- Use CAST(x AS VARCHAR) or CONVERT() for type conversions
"""
