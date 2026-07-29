from app.data_sources.clients.base import DataSourceClient
import pandas as pd
import sqlalchemy

from app.data_sources.engine_pool import get_engine
from app.data_sources.query_cancellation import track
from sqlalchemy import text
from contextlib import contextmanager
from typing import List, Generator
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property
from urllib.parse import quote_plus
import logging

logger = logging.getLogger(__name__)


class TrinoClient(DataSourceClient):
    def __init__(self, host, port, catalog, schema, user, password=None, protocol="http"):
        self.host = host
        self.port = port
        self.catalog = catalog
        self.schema = schema
        self.user = user
        self.password = password
        self.protocol = protocol

    @cached_property
    def trino_uri(self):
        if self.protocol == "https":
            if not self.password:
                raise ValueError("Password is required for HTTPS connections.")
            return f"trino://{quote_plus(self.user)}:{quote_plus(self.password)}@{self.host}:{self.port}/{self.catalog}/{self.schema}"
        return f"trino://{quote_plus(self.user)}@{self.host}:{self.port}/{self.catalog}/{self.schema}"

    @contextmanager
    def connect(self) -> Generator[sqlalchemy.engine.base.Connection, None, None]:
        conn = None
        try:
            engine = get_engine(self.trino_uri)
            conn = engine.connect()
        except Exception as e:
            logger.error(f"Error connecting to Trino: {e}")
            if conn is not None:
                conn.close()
            raise RuntimeError(f"{e}")
        # The yield is deliberately OUTSIDE the try/except above. With it
        # inside, this contextmanager caught whatever the *caller* raised in
        # its `with client.connect()` body and re-raised it as a bare
        # RuntimeError, erasing the type: an extraction abort came back
        # indistinguishable from a connection failure. The except clause is
        # meant to wrap connect-time errors, and now only does.
        try:
            with track(self, conn):
                yield conn
        finally:
            conn.close()
        # NB: no engine.dispose() — the engine is pooled and shared
        # (engine_pool). conn.close() above returns the connection.

    def execute_query(self, sql: str) -> pd.DataFrame:
        try:
            with self.connect() as conn:
                return pd.read_sql(text(sql), conn)
        except Exception as e:
            logger.error(f"Error executing SQL query: {e}")
            raise RuntimeError(f"{e}")

    def get_tables(self) -> List[Table]:
        try:
            with self.connect() as conn:
                sql = """
                    SELECT table_name, column_name, data_type
                    FROM information_schema.columns
                    WHERE table_catalog = :catalog
                    AND table_schema = :schema
                    ORDER BY table_name, ordinal_position
                """
                result = conn.execute(
                    text(sql), {"catalog": self.catalog, "schema": self.schema}
                ).fetchall()

                tables = {}
                for row in result:
                    table_name, column_name, data_type = row
                    if table_name not in tables:
                        tables[table_name] = Table(name=table_name, columns=[], pks=None, fks=None)
                    tables[table_name].columns.append(TableColumn(name=column_name, dtype=data_type))
                return list(tables.values())
        except Exception as e:
            logger.error(f"Error retrieving tables: {e}")
            return []

    def get_schema(self, table_id: str) -> Table:
        raise NotImplementedError("get_schema() is obsolete. Use get_tables() instead.")

    def get_schemas(self):
        return self.get_tables()

    def prompt_schema(self):
        return TableFormatter(self.get_tables()).table_str

    def test_connection(self):
        try:
            with self.connect() as conn:
                conn.execute(text("SELECT 1"))
                return {"success": True, "message": "Successfully connected to Trino"}
        except Exception as e:
            logger.error(f"Error testing Trino connection: {e}")
            return {"success": False, "message": str(e)}

    @property
    def description(self):
        system_prompt = """
        You can call the execute_query method to run SQL queries.

        ```python
        df = client.execute_query("SELECT * FROM users")
        ```
        or:
        ```python
        df = client.execute_query("SELECT * FROM users WHERE age > 30")
        ```
        """
        return f"Trino database at {self.host}:{self.port}/{self.catalog}/{self.schema}\n\n{system_prompt}"
