"""PostgreSQL client for the edge agent.

Follows the convention in `backend/app/data_sources/clients/postgresql_client.py`
— same method names, same return shapes — minus the pieces that only mean
something inside the control plane (quota metering, the shared engine pool, the
cancellation registry). Those arrive in the phase that adds dispatch.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from functools import cached_property
from typing import Any, Generator, Optional
from urllib.parse import quote_plus

import pandas as pd
import sqlalchemy
from sqlalchemy import create_engine, text

from .base import DataSourceClient
from .models import Table, TableColumn

logger = logging.getLogger(__name__)


class PostgresqlClient(DataSourceClient):

    relative_date_hint = (
        "Relative dates (PostgreSQL): CURRENT_DATE, CURRENT_DATE - INTERVAL '7 days', "
        "date_trunc('month', CURRENT_DATE); the clock is the DB server's."
    )

    def __init__(
        self,
        host: str,
        port: int = 5432,
        database: str = "postgres",
        user: str = "postgres",
        password: str = "",
        schema: Optional[str] = None,
        connect_timeout: int = 10,
    ):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.schema = schema
        self.connect_timeout = connect_timeout

        # Optional schema, or a comma-separated list. Deduped, order preserved.
        self._schemas: list[str] = []
        if isinstance(schema, str) and schema.strip():
            seen = set()
            for part in (s.strip() for s in schema.split(",")):
                if part and part not in seen:
                    seen.add(part)
                    self._schemas.append(part)

    @cached_property
    def pg_uri(self) -> str:
        # Quote the credentials: a password containing '@' or '/' silently
        # corrupts the URI otherwise, and the resulting error names the host
        # rather than the real cause.
        return (
            f"postgresql+psycopg2://{quote_plus(self.user)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    @cached_property
    def _engine(self) -> sqlalchemy.engine.Engine:
        return create_engine(
            self.pg_uri,
            pool_pre_ping=True,
            connect_args={"connect_timeout": self.connect_timeout},
        )

    @contextmanager
    def connect(self) -> Generator[sqlalchemy.engine.base.Connection, None, None]:
        """Yield a connection to the database."""
        conn = None
        try:
            conn = self._engine.connect()
            if self._schemas:
                try:
                    conn.execute(text(f"SET search_path TO {', '.join(self._schemas)}"))
                except Exception:
                    pass
        except Exception as e:
            if conn is not None:
                conn.close()
            raise RuntimeError(f"{e}") from e

        # Deliberately outside the try above: with it inside, this would catch
        # whatever the *caller* raised in its `with` body and re-raise it as a
        # bare RuntimeError, making a query error indistinguishable from a
        # connection failure.
        try:
            yield conn
        finally:
            conn.close()

    def execute_query(self, sql: str, **kwargs) -> pd.DataFrame:
        """Execute SQL and return the result as a DataFrame."""
        with self.connect() as conn:
            return pd.read_sql(text(sql), conn)

    def test_connection(self) -> dict[str, Any]:
        try:
            with self.connect() as conn:
                version = conn.execute(text("SELECT version()")).scalar()
            return {"success": True, "message": f"Connected to {version}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_tables(self) -> list[Table]:
        """Columns and primary keys for every table in scope.

        One round trip per concern rather than per table: a catalog with a few
        thousand tables makes the per-table version pathological, and this runs
        on every schema index.
        """
        where = "c.table_schema NOT IN ('information_schema', 'pg_catalog')"
        params: dict[str, Any] = {}
        if self._schemas:
            keys = [f"s{i}" for i in range(len(self._schemas))]
            where = f"c.table_schema IN ({', '.join(':' + k for k in keys)})"
            params = dict(zip(keys, self._schemas))

        columns_sql = text(f"""
            SELECT c.table_schema, c.table_name, c.column_name, c.data_type
            FROM information_schema.columns c
            JOIN information_schema.tables t
              ON t.table_schema = c.table_schema AND t.table_name = c.table_name
            WHERE {where} AND t.table_type IN ('BASE TABLE', 'VIEW')
            ORDER BY c.table_schema, c.table_name, c.ordinal_position
        """)
        pks_sql = text(f"""
            SELECT c.table_schema, c.table_name, c.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage c
              ON c.constraint_name = tc.constraint_name
             AND c.table_schema = tc.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY' AND {where}
        """)

        tables: dict[str, Table] = {}
        with self.connect() as conn:
            for schema, table, column, dtype in conn.execute(columns_sql, params):
                key = f"{schema}.{table}"
                tables.setdefault(key, Table(name=key))
                tables[key].columns.append(TableColumn(name=column, dtype=dtype))

            for schema, table, column in conn.execute(pks_sql, params):
                t = tables.get(f"{schema}.{table}")
                if t is not None:
                    t.pks.append(TableColumn(name=column))

        return list(tables.values())

    def get_schemas(self) -> list[Table]:
        return self.get_tables()

    def prompt_schema(self) -> str:
        lines = []
        for t in self.get_schemas():
            cols = ", ".join(f"{c.name} {c.dtype or ''}".strip() for c in t.columns)
            lines.append(f"{t.name}({cols})")
        return "\n".join(lines)
