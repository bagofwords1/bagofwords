from app.data_sources.clients.base import DataSourceClient

import pandas as pd
from typing import List, Generator, Optional, Dict, Any
from contextlib import contextmanager
from app.ai.prompt_formatters import Table, TableColumn, TableFormatter


class DruidClient(DataSourceClient):
    """Apache Druid client (SQL via the Broker/Router SQL HTTP endpoint).

    Druid speaks Calcite SQL over an HTTP endpoint (default
    ``/druid/v2/sql/``). We use the ``pydruid`` DB-API driver to run queries
    and discover the catalog from ``INFORMATION_SCHEMA.COLUMNS`` — the same
    metadata approach the engine exposes to any SQL client. Datasources live
    in the ``druid`` schema; the ``sys`` and ``INFORMATION_SCHEMA`` schemas
    are Druid internals and are always excluded from discovery.
    """

    # Druid-internal schemas that should never surface as user tables.
    SYSTEM_SCHEMAS = {"INFORMATION_SCHEMA", "sys"}

    def __init__(
        self,
        host: str,
        port: int = 8082,
        user: Optional[str] = None,
        password: Optional[str] = None,
        token: Optional[str] = None,
        secure: bool = False,
        path: str = "/druid/v2/sql/",
        schema: Optional[str] = None,
        ssl_verify_cert: bool = True,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.token = token
        self.secure = secure
        self.path = path
        self.schema = schema
        self.ssl_verify_cert = ssl_verify_cert

        # Optional schema filter: comma-separated, deduped, order-preserved.
        self._schemas: List[str] = []
        if isinstance(self.schema, str) and self.schema.strip():
            seen = set()
            for part in self.schema.split(","):
                s = part.strip()
                if s and s not in seen:
                    seen.add(s)
                    self._schemas.append(s)

        # Kwargs for pydruid.db.connect. The driver is imported lazily in
        # connect() so the module imports even when pydruid isn't installed.
        self._connect_kwargs: Dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "scheme": ("https" if self.secure else "http"),
            "ssl_verify_cert": self.ssl_verify_cert,
        }
        # Auth selection: a bearer token (e.g. Imply Polaris API token) maps to
        # pydruid's ``jwt`` kwarg (Authorization: Bearer <token>). The driver
        # prefers Basic over Bearer when ``user`` is set, so token auth must NOT
        # also send user/password — they are mutually exclusive here, token wins.
        if self.token:
            self._connect_kwargs["jwt"] = self.token
        else:
            if self.user:
                self._connect_kwargs["user"] = self.user
            if self.password:
                self._connect_kwargs["password"] = self.password

    @staticmethod
    def _quote_literal(value: str) -> str:
        """Render a Python string as a safe Druid SQL string literal."""
        return "'" + str(value).replace("'", "''") + "'"

    @contextmanager
    def connect(self) -> Generator[Any, None, None]:
        from pydruid.db import connect as druid_connect

        conn = None
        try:
            conn = druid_connect(**self._connect_kwargs)
            yield conn
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute a Druid SQL statement and return the result as a DataFrame."""
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
        """Discover datasources and their columns via INFORMATION_SCHEMA.

        Each Druid datasource maps to a Table named ``schema.datasource``
        (typically ``druid.<name>``), with column data types as reported by
        the engine.
        """
        # Druid SQL identifiers/values here come from admin-provided config
        # (schema names), not end-user input. pydruid's DB-API does not accept
        # qmark/positional params, so we inline single-quote-escaped string
        # literals — the same approach the Pinot client uses.
        if self._schemas:
            in_list = ", ".join(self._quote_literal(s) for s in self._schemas)
            where_sql = f" WHERE TABLE_SCHEMA IN ({in_list})"
        else:
            # Exclude Druid-internal schemas when no explicit filter is given.
            in_list = ", ".join(self._quote_literal(s) for s in sorted(self.SYSTEM_SCHEMAS))
            where_sql = f" WHERE TABLE_SCHEMA NOT IN ({in_list})"

        sql = (
            "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE "
            "FROM INFORMATION_SCHEMA.COLUMNS"
            f"{where_sql} "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
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
                    metadata_json={"schema": table_schema},
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
            return {"success": True, "message": "Successfully connected to Druid"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    @property
    def description(self):
        scheme = "https" if self.secure else "http"
        parts = [f"Apache Druid SQL endpoint at {scheme}://{self.host}:{self.port}{self.path}"]
        if self._schemas:
            parts.append(f"schemas={', '.join(self._schemas)}")
        parts.append(
            "You can call the execute_query method to run Druid SQL queries, e.g. "
            "client.execute_query('SELECT __time, COUNT(*) FROM druid.wikipedia "
            "GROUP BY 1'). Datasources live in the \"druid\" schema; always "
            "filter on the __time column to keep scans bounded."
        )
        return " | ".join(parts)
