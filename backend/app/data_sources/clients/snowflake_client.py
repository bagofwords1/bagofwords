from app.data_sources.clients.base import DataSourceClient

import pandas as pd
import sqlalchemy
from sqlalchemy import text
from contextlib import contextmanager
from typing import Generator, List
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property
from snowflake.sqlalchemy import URL


class SnowflakeClient(DataSourceClient):
    def __init__(self, account, user, password, warehouse, database, schema):
        self.account = account
        self.user = user
        self.password = password
        self.database = database
        # Accept comma-separated schemas in the existing `schema` field
        # Normalize to uppercase per Snowflake INFORMATION_SCHEMA behavior
        self.schema = schema
        self._schemas = []
        if isinstance(self.schema, str) and self.schema.strip():
            parts = [s.strip() for s in self.schema.split(",") if s.strip()]
            # Dedupe while preserving order
            seen = set()
            for p in parts:
                up = p.upper()
                if up not in seen:
                    seen.add(up)
                    self._schemas.append(up)
        # Primary schema for connection string (fall back to provided single schema)
        self._primary_schema = (self._schemas[0] if self._schemas else (self.schema.upper() if isinstance(self.schema, str) and self.schema else None))
        self.warehouse = warehouse

    @cached_property
    def snowflake_uri(self):
        # Always omit schema in the URL; we use fully-qualified names (SCHEMA.TABLE) in queries
        uri = (
            f"snowflake://{self.user}:{self.password}@{self.account}/"
            f"{self.database}?warehouse={self.warehouse}"
        )
        return uri

    @contextmanager
    def connect(self) -> Generator[sqlalchemy.engine.base.Connection, None, None]:
        """Yield a connection to a Snowflake database."""
        engine = None
        conn = None

        try:
            engine = sqlalchemy.create_engine(self.snowflake_uri)
            conn = engine.connect()
            yield conn
        except Exception as e:
            raise RuntimeError(f"Error while connecting to Snowflake: {e}")

        finally:
            if conn is not None:
                conn.close()
            if engine is not None:
                engine.dispose()

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Run SQL statement."""
        try:
            with self.connect() as conn:
                # Wrap SQL query with text() to handle complex SQL
                df = pd.read_sql(text(sql), conn)
            return df
        except Exception as e:
            print(f"Error executing SQL: {e}")
            raise

    def get_tables(self) -> List[Table]:
        """Get all tables and their columns across one or more schemas.
        - Supports comma-separated schemas via the existing `schema` config field.
        - Always emits fully qualified table names: SCHEMA.TABLE
        """
        tables = {}
        with self.connect() as conn:
            # Build WHERE clause for single vs multi schema
            params = {}
            where_clauses = []
            if self._schemas:
                in_keys = []
                for idx, sch in enumerate(self._schemas):
                    key = f"s{idx}"
                    in_keys.append(f":{key}")
                    params[key] = sch
                where_clauses.append(f"table_schema IN ({', '.join(in_keys)})")
            elif self._primary_schema:
                params["schema"] = self._primary_schema
                where_clauses.append("table_schema = :schema")

            where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
            sql = text(f"""
                SELECT table_schema, table_name, column_name, data_type
                FROM {self.database}.INFORMATION_SCHEMA.COLUMNS
                {where_sql}
                ORDER BY table_schema, table_name, ordinal_position
            """)

            results = conn.execute(sql, params).fetchall()

            for row in results:
                table_schema, table_name, column_name, data_type = row
                key = (table_schema, table_name)
                fqn = f"{table_schema}.{table_name}"
                if key not in tables:
                    tables[key] = Table(
                        name=fqn, columns=[], pks=None, fks=None, metadata_json={"schema": table_schema}
                    )
                tables[key].columns.append(TableColumn(name=column_name, dtype=data_type))

        return list(tables.values())

    def get_schema(self, table: str, schema: str) -> Table:
        """Return Table."""
        with self.connect() as conn:
            columns = []
            sql = text(f"SHOW COLUMNS IN {schema}.{table}")
            schema_list = conn.execute(sql).fetchall()

            for row in schema_list:
                columns.append(TableColumn(name=row[0], dtype=row[1]))

            return Table(name=f"{schema}.{table}", columns=columns, pks=None, fks=None, metadata_json={"schema": schema})

    def get_schemas(self):
        tables = self.get_tables()
        return tables

    def prompt_schema(self):
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        """Test database connection and return status information."""
        try:
            with self.connect() as conn:
                conn.execute(text("SELECT 1"))
                return {
                    "success": True,
                    "message": "Successfully connected to database"
                }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @property
    def description(self):
        description = f"Snowflake database {
            self.database} on account {self.account}"
        return description
