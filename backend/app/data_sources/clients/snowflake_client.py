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
        self.schema = schema
        self.warehouse = warehouse

    @cached_property
    def snowflake_uri(self):
        uri = (
            f"snowflake://{self.user}:{self.password}@{self.account}/"
            f"{self.database}/{self.schema}?warehouse={self.warehouse}"
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
        """Get all tables and their columns in the database and schema."""
        tables = {}
        with self.connect() as conn:
            # Dynamically build the fully qualified name for INFORMATION_SCHEMA
            sql = text(f"""
                SELECT table_schema, table_name, column_name, data_type
                FROM {self.database}.INFORMATION_SCHEMA.COLUMNS
                WHERE table_schema = :schema
                ORDER BY table_schema, table_name, ordinal_position
            """)

            results = conn.execute(sql, {'schema': self.schema}).fetchall()

            for row in results:
                schema, table, column_name, data_type = row
                if (schema, table) not in tables:
                    tables[(schema, table)] = Table(
                        name=table, columns=[], pks=None, fks=None)
                tables[(schema, table)].columns.append(
                    TableColumn(name=column_name, dtype=data_type))

        return list(tables.values())

    def get_schema(self, table: str, schema: str) -> Table:
        """Return Table."""
        with self.connect() as conn:
            columns = []
            sql = text(f"SHOW COLUMNS IN {schema}.{table}")
            schema_list = conn.execute(sql).fetchall()

            for row in schema_list:
                columns.append(TableColumn(name=row[0], dtype=row[1]))

            return Table(name=table, columns=columns, pks=None, fks=None)

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
