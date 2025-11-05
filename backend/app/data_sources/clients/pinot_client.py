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
            print(f"Error executing SQL: {e}")
            raise

    def get_tables(self) -> List[Table]:
        # Strategy A: INFORMATION_SCHEMA via SQL (recent Pinot versions)
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                list_sql = "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES"
                if self.query_options:
                    cursor.execute(list_sql, queryOptions=self.query_options)
                else:
                    cursor.execute(list_sql)
                table_names = [row[0] for row in cursor.fetchall()]

                tables: Dict[str, Table] = {}
                for t in table_names:
                    cols_sql = (
                        f"SELECT COLUMN_NAME, DATA_TYPE "
                        f"FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = '{t}'"
                    )
                    if self.query_options:
                        cursor.execute(cols_sql, queryOptions=self.query_options)
                    else:
                        cursor.execute(cols_sql)
                    cols = cursor.fetchall()
                    table = Table(name=t, columns=[], pks=[], fks=[], metadata_json={})
                    for col_name, dtype in cols:
                        table.columns.append(TableColumn(name=col_name, dtype=dtype))
                    tables[t] = table
                cursor.close()
                return list(tables.values())
        except Exception:
            # Strategy B: Controller REST (if configured)
            if not self.controller or not requests:
                return []
            try:
                base = self.controller.rstrip("/")
                r = requests.get(f"{base}/tables", timeout=10)
                r.raise_for_status()
                payload = r.json()
                table_names = payload.get("tables", []) if isinstance(payload, dict) else payload

                tables: Dict[str, Table] = {}
                for t in table_names or []:
                    columns: List[TableColumn] = []
                    # Attempt schema lookup
                    try:
                        cfg = requests.get(f"{base}/tables/{t}", timeout=10)
                        cfg.raise_for_status()
                        cfg_json = cfg.json()
                        candidate = cfg_json.get("OFFLINE") or cfg_json.get("REALTIME") or {}
                        schema_name = (
                            candidate.get("tableConfig", {})
                            .get("validationConfig", {})
                            .get("schemaName")
                        )
                        if schema_name:
                            sch = requests.get(f"{base}/schemas/{schema_name}", timeout=10)
                            if sch.ok:
                                sj = sch.json()
                                for sec in (
                                    "dimensionFieldSpecs",
                                    "metricFieldSpecs",
                                    "dateTimeFieldSpecs",
                                    "timeFieldSpec",
                                ):
                                    fields = sj.get(sec) or []
                                    if isinstance(fields, dict):
                                        fields = [fields]
                                    for f in fields:
                                        name = f.get("name")
                                        dtype = f.get("dataType") or f.get("dataTypeName") or "STRING"
                                        if name:
                                            columns.append(TableColumn(name=name, dtype=dtype))
                    except Exception:
                        pass
                    tables[t] = Table(name=t, columns=columns, pks=[], fks=[], metadata_json={})
                return list(tables.values())
            except Exception:
                return []

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


