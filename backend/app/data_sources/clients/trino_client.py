from app.data_sources.clients.base import DataSourceClient

import pandas as pd
from typing import List, Generator, Optional, Dict, Any
from contextlib import contextmanager
from app.ai.prompt_formatters import Table, TableColumn, TableFormatter


class TrinoClient(DataSourceClient):
    """Trino client (SQL via the Trino DB-API driver).

    Trino is a distributed SQL query engine (the successor to PrestoSQL). A
    connection is bound to a single ``catalog``; tables are discovered from
    that catalog's ``information_schema.columns`` and named ``schema.table``.
    An optional comma-separated ``schema`` filter narrows discovery; otherwise
    every schema except ``information_schema`` is indexed.

    The ``trino`` driver is imported lazily inside ``connect()`` so the module
    imports without the dependency installed and the unit tests can inject a
    fake driver.
    """

    # Always-present metadata schema that should not surface as a user table.
    SYSTEM_SCHEMAS = {"information_schema"}

    def __init__(
        self,
        host: str,
        port: int = 8080,
        catalog: Optional[str] = None,
        schema: Optional[str] = None,
        user: str = "trino",
        password: Optional[str] = None,
        http_scheme: str = "http",
    ):
        self.host = host
        self.port = port
        self.catalog = catalog
        self.schema = schema
        self.user = user or "trino"
        self.password = password
        # Basic auth requires TLS; default to https when a password is supplied.
        self.http_scheme = "https" if (password and http_scheme == "http") else http_scheme

        # Optional schema filter: comma-separated, deduped, order-preserved.
        self._schemas: List[str] = []
        if isinstance(self.schema, str) and self.schema.strip():
            seen = set()
            for part in self.schema.split(","):
                s = part.strip()
                if s and s not in seen:
                    seen.add(s)
                    self._schemas.append(s)

    @staticmethod
    def _quote_literal(value: str) -> str:
        """Render a Python string as a safe SQL string literal."""
        return "'" + str(value).replace("'", "''") + "'"

    @staticmethod
    def _quote_ident(value: str) -> str:
        """Render a Python string as a safe SQL double-quoted identifier."""
        return '"' + str(value).replace('"', '""') + '"'

    @contextmanager
    def connect(self) -> Generator[Any, None, None]:
        from trino.dbapi import connect as trino_connect

        kwargs: Dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "http_scheme": self.http_scheme,
        }
        if self.catalog:
            kwargs["catalog"] = self.catalog
        if self.schema and len(self._schemas) == 1:
            # A single default schema can be set on the session.
            kwargs["schema"] = self._schemas[0]
        if self.password:
            from trino.auth import BasicAuthentication
            kwargs["auth"] = BasicAuthentication(self.user, self.password)

        conn = None
        try:
            conn = trino_connect(**kwargs)
            yield conn
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute a Trino SQL statement and return the result as a DataFrame."""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute(sql)
                rows = cursor.fetchall()
                cols = (
                    [d[0] for d in cursor.description]
                    if getattr(cursor, "description", None)
                    else []
                )
                try:
                    cursor.close()
                except Exception:
                    pass
                return pd.DataFrame(rows, columns=cols or None)
        except Exception as e:
            print(f"Error executing SQL: {e}")
            raise

    def get_tables(self) -> List[Table]:
        """Discover tables and columns via the catalog's information_schema."""
        # information_schema lives under the connection's catalog.
        if self.catalog:
            columns_ref = f"{self._quote_ident(self.catalog)}.information_schema.columns"
        else:
            columns_ref = "information_schema.columns"

        if self._schemas:
            in_list = ", ".join(self._quote_literal(s) for s in self._schemas)
            where_sql = f" WHERE table_schema IN ({in_list})"
        else:
            in_list = ", ".join(self._quote_literal(s) for s in sorted(self.SYSTEM_SCHEMAS))
            where_sql = f" WHERE table_schema NOT IN ({in_list})"

        sql = (
            "SELECT table_schema, table_name, column_name, data_type "
            f"FROM {columns_ref}"
            f"{where_sql} "
            "ORDER BY table_schema, table_name, ordinal_position"
        )

        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute(sql)
                result = cursor.fetchall()
                try:
                    cursor.close()
                except Exception:
                    pass
        except Exception as e:
            print(f"Error retrieving tables: {e}")
            return []

        tables: Dict[tuple, Table] = {}
        for row in result:
            table_schema, table_name, column_name, data_type = row[0], row[1], row[2], row[3]
            key = (table_schema, table_name)
            fqn = f"{table_schema}.{table_name}"
            if key not in tables:
                tables[key] = Table(
                    name=fqn,
                    columns=[],
                    pks=[],
                    fks=[],
                    metadata_json={"schema": table_schema, "catalog": self.catalog},
                )
            tables[key].columns.append(TableColumn(name=column_name, dtype=data_type))
        return list(tables.values())

    def get_schema(self, table: str) -> Table:
        raise NotImplementedError("get_schema() is obsolete. Use get_tables() instead.")

    def get_schemas(self):
        return self.get_tables()

    def prompt_schema(self):
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        try:
            self.execute_query("SELECT 1")
            return {"success": True, "message": "Successfully connected to Trino"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    @property
    def description(self):
        parts = [f"Trino query engine at {self.http_scheme}://{self.host}:{self.port}"]
        if self.catalog:
            parts.append(f"catalog={self.catalog}")
        if self._schemas:
            parts.append(f"schemas={', '.join(self._schemas)}")
        parts.append(
            "You can call the execute_query method to run Trino SQL, e.g. "
            "client.execute_query('SELECT * FROM schema.table LIMIT 100'). "
            "Tables are referenced as schema.table within the connected catalog."
        )
        return " | ".join(parts)
