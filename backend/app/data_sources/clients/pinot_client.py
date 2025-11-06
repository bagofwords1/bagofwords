from app.data_sources.clients.base import DataSourceClient

import pandas as pd
from typing import List, Generator, Optional, Dict, Any
from contextlib import contextmanager
from app.ai.prompt_formatters import Table, TableColumn, TableFormatter

from pinotdb import connect as pinot_connect

try:
    import requests  # type: ignore
except Exception:
    requests = None  # graceful fallback when requests is unavailable


class PinotClient(DataSourceClient):
    def __init__(
        self,
        host: str,
        port: int,
        user: Optional[str] = None,
        password: Optional[str] = None,
        secure: bool = True,
        path: str = "/query/sql",
        controller: Optional[str] = None,  # e.g. "http://controller-host:9000"
        query_options: Optional[str] = None,
        database: Optional[str] = None,  # Pinot does not use database; for parity only
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.secure = secure
        self.path = path
        self.controller = controller
        self.query_options = query_options
        self.database = database

        # Prepare kwargs for pinotdb.connect
        self._connect_kwargs: Dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "scheme": ("https" if self.secure else "http"),
        }
        if self.user:
            self._connect_kwargs["username"] = self.user
        if self.password:
            self._connect_kwargs["password"] = self.password
        if self.controller:
            # Expected form: http://controller-host:9000
            self._connect_kwargs["controller"] = self.controller

    @contextmanager
    def connect(self) -> Generator[Any, None, None]:
        conn = None
        try:
            conn = pinot_connect(**self._connect_kwargs)
            yield conn
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def execute_query(self, sql: str) -> pd.DataFrame:
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                if self.query_options:
                    cursor.execute(sql, queryOptions=self.query_options)
                else:
                    cursor.execute(sql)
                rows = cursor.fetchall()
                cols = [d[0] for d in (cursor.description or [])] if getattr(cursor, "description", None) else []
                cursor.close()
                return pd.DataFrame(rows, columns=cols or None)
        except Exception as e:
            breakpoint()
            print(f"Error executing SQL: {e}")
            raise

    def get_tables(self) -> List[Table]:
        # Simple discovery: controller list + LIMIT 0 inference, else INFORMATION_SCHEMA + LIMIT 0
        tables: Dict[str, Table] = {}
        table_names: List[str] = []

        if self.controller and requests:
            try:
                base = self.controller.rstrip("/")
                r = requests.get(f"{base}/tables", timeout=10)
                r.raise_for_status()
                payload = r.json()
                table_names = payload.get("tables", []) if isinstance(payload, dict) else list(payload or [])
            except Exception:
                table_names = []

        if not table_names:
            try:
                with self.connect() as conn:
                    cursor = conn.cursor()
                    list_sql = "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES"
                    if self.query_options:
                        cursor.execute(list_sql, queryOptions=self.query_options)
                    else:
                        cursor.execute(list_sql)
                    table_names = [row[0] for row in cursor.fetchall()]
                    cursor.close()
            except Exception:
                return []

        for t in table_names:
            columns: List[TableColumn] = []
            try:
                probe_sql = f"SELECT * FROM {t} LIMIT 0"
                with self.connect() as conn:
                    cursor = conn.cursor()
                    if self.query_options:
                        cursor.execute(probe_sql, queryOptions=self.query_options)
                    else:
                        cursor.execute(probe_sql)
                    inferred = [d[0] for d in (cursor.description or [])] if getattr(cursor, "description", None) else []
                    cursor.close()
                    for c in inferred:
                        columns.append(TableColumn(name=c, dtype="STRING"))
            except Exception:
                pass
            tables[t] = Table(name=t, columns=columns, pks=[], fks=[], metadata_json={})
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
            return {"success": True, "message": "Successfully connected to Pinot"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    @property
    def description(self):
        scheme = "https" if self.secure else "http"
        parts = [f"Pinot broker at {scheme}://{self.host}:{self.port}{self.path}"]
        if self.controller:
            parts.append(f"controller={self.controller}")
        if self.query_options:
            parts.append(f"queryOptions={self.query_options}")
        return " | ".join(parts)


