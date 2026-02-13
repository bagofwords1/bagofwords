from app.data_sources.clients.base import DataSourceClient

import pandas as pd
from contextlib import contextmanager
from typing import Generator, List, Optional
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property

from databricks import sql as databricks_sql


class DatabricksSqlClient(DataSourceClient):
    """Client for Databricks SQL Warehouse connections."""

    def __init__(
        self,
        server_hostname: str,
        http_path: str,
        access_token: str,
        catalog: str,
        schema: Optional[str] = None,
    ):
        self.server_hostname = server_hostname
        self.http_path = http_path
        self.access_token = access_token
        self.catalog = catalog
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

    @contextmanager
    def connect(self) -> Generator:
        """Yield a connection to a Databricks SQL Warehouse."""
        conn = None
        try:
            conn = databricks_sql.connect(
                server_hostname=self.server_hostname,
                http_path=self.http_path,
                access_token=self.access_token,
                catalog=self.catalog,
            )
            yield conn
        except Exception as e:
            raise RuntimeError(f"Error connecting to Databricks SQL: {e}")
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
                df = pd.DataFrame(rows, columns=columns)
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
        """Get tables with column/table comments. May fail on some Databricks configurations."""
        tables = {}
        with self.connect() as conn:
            cursor = conn.cursor()

            where_clauses = [f"c.table_catalog = '{self.catalog}'"]
            if self.catalog.lower() != 'system':
                where_clauses.append("c.table_schema NOT IN ('information_schema', 'default')")
            if self._schemas:
                schema_list = ", ".join([f"'{s}'" for s in self._schemas])
                where_clauses.append(f"c.table_schema IN ({schema_list})")

            where_sql = " WHERE " + " AND ".join(where_clauses)

            sql = f"""
                SELECT
                    c.table_schema,
                    c.table_name,
                    c.column_name,
                    c.data_type,
                    c.comment AS column_comment,
                    t.comment AS table_comment
                FROM system.information_schema.columns c
                LEFT JOIN system.information_schema.tables t
                    ON c.table_catalog = t.table_catalog
                    AND c.table_schema = t.table_schema
                    AND c.table_name = t.table_name
                {where_sql}
                ORDER BY c.table_schema, c.table_name, c.ordinal_position
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
                        metadata_json={"schema": table_schema, "catalog": self.catalog}
                    )
                tables[key].columns.append(TableColumn(
                    name=column_name,
                    dtype=data_type,
                    description=col_comment if col_comment else None
                ))

        return list(tables.values())

    def _get_tables_basic(self) -> List[Table]:
        """Get tables without comments (original query - always works)."""
        tables = {}
        with self.connect() as conn:
            cursor = conn.cursor()

            where_clauses = [f"table_catalog = '{self.catalog}'"]
            if self.catalog.lower() != 'system':
                where_clauses.append("table_schema NOT IN ('information_schema', 'default')")
            if self._schemas:
                schema_list = ", ".join([f"'{s}'" for s in self._schemas])
                where_clauses.append(f"table_schema IN ({schema_list})")

            where_sql = " WHERE " + " AND ".join(where_clauses)

            sql = f"""
                SELECT table_schema, table_name, column_name, data_type
                FROM system.information_schema.columns
                {where_sql}
                ORDER BY table_schema, table_name, ordinal_position
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
                        metadata_json={"schema": table_schema, "catalog": self.catalog}
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
        """Test connection to Databricks SQL Warehouse and return status information."""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                cursor.close()
                return {
                    "success": True,
                    "message": "Successfully connected to Databricks SQL Warehouse"
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
        return f"""Databricks SQL Warehouse
Server: {self.server_hostname}
Catalog: {self.catalog}
Schemas: {schema_info}

You can execute SQL queries using the execute_query method:
```python
df = client.execute_query("SELECT * FROM schema.table_name")
```

Databricks SQL uses standard SQL syntax with some extensions.
Tables are organized in a three-level namespace: catalog.schema.table
"""
