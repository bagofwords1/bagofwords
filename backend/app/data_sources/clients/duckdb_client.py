from app.data_sources.clients.base import DataSourceClient

import duckdb
import pandas as pd
from contextlib import contextmanager
from typing import Generator, List
from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
import urllib.parse


class DuckDBClient(DataSourceClient):
    def __init__(self,
                 uris: str | None = None,
                 database: str | None = None,
                 # none auth
                 # aws
                 access_key: str | None = None,
                 secret_key: str | None = None,
                 region: str | None = None,
                 session_token: str | None = None,
                 # gcp
                 service_account_json: str | None = None,
                 # azure
                 connection_string: str | None = None,
                 ):
        self.uris_raw = uris or ""
        self.database = database  # Path to local .duckdb file
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.service_account_json = service_account_json
        self.connection_string = connection_string
        self.session_token = session_token

        # normalize list of URI patterns (one per line)
        self.uri_patterns: List[str] = [u.strip() for u in (self.uris_raw.splitlines() if self.uris_raw else []) if u.strip()]

        # in-memory connection is fine; each connect() manages its own handle
        self._con: duckdb.DuckDBPyConnection | None = None

    def _sql_literal(self, value: str | None) -> str:
        if value is None:
            return "NULL"
        # escape single quotes by doubling them
        return "'" + str(value).replace("'", "''") + "'"

    def _quote_identifier(self, identifier: str) -> str:
        """Quote an identifier (table/view/column name) to prevent SQL injection.

        In DuckDB, identifiers are quoted with double quotes, and any double quotes
        within the identifier are escaped by doubling them.
        """
        if identifier is None:
            raise ValueError("Identifier cannot be None")
        # Escape double quotes by doubling them
        escaped = str(identifier).replace('"', '""')
        return f'"{escaped}"'

    def _configure_httpfs(self, con: duckdb.DuckDBPyConnection) -> None:
        con.execute("INSTALL httpfs;")
        con.execute("LOAD httpfs;")
        # Prefer path-style addressing to avoid some bucket policy issues
        con.execute("SET s3_url_style='path';")
        con.execute("SET s3_use_ssl=true;")
        # Azure support (ADLS/Blob)
        try:
            con.execute("INSTALL azure;")
            con.execute("LOAD azure;")
            con.execute("SET azure_transport_option_type='curl';")
        except Exception:
            # Ignore if azure extension is unavailable in this build
            pass

        # AWS
        if self.access_key and self.secret_key:
            con.execute(f"SET s3_access_key_id={self._sql_literal(self.access_key)};")
            con.execute(f"SET s3_secret_access_key={self._sql_literal(self.secret_key)};")
            if self.region:
                con.execute(f"SET s3_region={self._sql_literal(self.region)};")
            if self.session_token:
                con.execute(f"SET s3_session_token={self._sql_literal(self.session_token)};")

        # GCP
        if self.service_account_json:
            # DuckDB accepts JSON credentials via setting
            con.execute(f"SET gcs_credentials={self._sql_literal(self.service_account_json)};")

        # Azure
        if self.connection_string:
            # DuckDB supports Azure via connection string (SAS/account key)
            con.execute(f"SET azure_storage_connection_string={self._sql_literal(self.connection_string)};")

    def _normalize_uri(self, pattern: str) -> str:
        """Trust user-supplied URIs.

        Pass-through schemes like s3://, gs://, file:/, abfss://, wasbs://, https://, and az://.
        DuckDB's Azure extension supports az:// directly, so no rewriting here.
        """
        return pattern

    def _safe_view_name(self, base: str, used: set[str]) -> str:
        import re
        name = re.sub(r"[^a-zA-Z0-9_]+", "_", base).strip("_") or "t"
        original = name
        i = 1
        while name in used:
            i += 1
            name = f"{original}_{i}"
        used.add(name)
        return name

    def _create_views(self, con: duckdb.DuckDBPyConnection) -> List[str]:
        created: List[str] = []
        used: set[str] = set()
        import os
        for pattern in self.uri_patterns:
            normalized = self._normalize_uri(pattern)
            # derive a friendly name from the last path segment (filename without extension)
            last = normalized.rstrip("/")
            last_segment = last.split("/")[-1] if "/" in last else last
            # if wildcard, fall back to parent directory name
            if "*" in last_segment or last_segment == "":
                # parent directory
                parent = last.rsplit("/", 1)[0] if "/" in last else last
                parent_segment = parent.split("/")[-1] if parent else "files"
                candidate = parent_segment
            else:
                # strip extension
                candidate = last_segment.rsplit(".", 1)[0]
            view = self._safe_view_name(candidate, used)
            lower = normalized.lower()
            quoted_view = self._quote_identifier(view)
            if lower.endswith(".parquet") or ".parquet" in lower:
                con.execute(f"CREATE OR REPLACE VIEW {quoted_view} AS SELECT * FROM read_parquet({self._sql_literal(normalized)})")
            else:
                # default to CSV auto
                con.execute(f"CREATE OR REPLACE VIEW {quoted_view} AS SELECT * FROM read_csv_auto({self._sql_literal(normalized)})")
            created.append(view)
        return created

    @contextmanager
    def connect(self) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        con: duckdb.DuckDBPyConnection | None = None
        try:
            if self.database:
                # Open local .duckdb file directly (read-only for safety)
                con = duckdb.connect(database=self.database, read_only=True)
            else:
                # In-memory database with views from URI patterns
                con = duckdb.connect(database=":memory:")
                self._configure_httpfs(con)
                self._create_views(con)
            yield con
        except Exception as e:
            raise RuntimeError(f"Error while connecting to DuckDB: {e}")
        finally:
            try:
                if con is not None:
                    con.close()
            except Exception:
                pass

    def execute_query(self, sql: str) -> pd.DataFrame:
        try:
            with self.connect() as con:
                res = con.execute(sql)
                return res.df()
        except Exception as e:
            raise

    def get_tables(self) -> List[Table]:
        tables: List[Table] = []
        with self.connect() as con:
            # For database files, list actual tables; for URI mode, list views
            if self.database:
                rows = con.execute("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'main' AND table_type IN ('BASE TABLE', 'VIEW')
                    ORDER BY table_name
                """).fetchall()
            else:
                # list views in main schema (URI mode)
                rows = con.execute("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'main' AND table_type = 'VIEW'
                    ORDER BY table_name
                """).fetchall()
            for (name,) in rows:
                cols = []
                quoted_name = self._quote_identifier(name)
                try:
                    desc = con.execute(f"DESCRIBE {quoted_name}").fetchall()
                    for d in desc:
                        # DuckDB DESCRIBE columns: column_name, column_type, null, key, default, extra
                        col_name = d[0]
                        col_type = d[1]
                        cols.append(TableColumn(name=col_name, dtype=str(col_type)))
                except Exception:
                    # Fallback: zero-row scan
                    try:
                        df = con.execute(f"SELECT * FROM {quoted_name} LIMIT 0").df()
                        for c in df.columns:
                            cols.append(TableColumn(name=str(c), dtype="unknown"))
                    except Exception:
                        pass
                tables.append(Table(name=name, columns=cols, pks=[], fks=[]))
        return tables

    def get_schema(self, table_name: str) -> Table:
        cols: List[TableColumn] = []
        with self.connect() as con:
            quoted_name = self._quote_identifier(table_name)
            try:
                desc = con.execute(f"DESCRIBE {quoted_name}").fetchall()
                for d in desc:
                    col_name = d[0]
                    col_type = d[1]
                    cols.append(TableColumn(name=col_name, dtype=str(col_type)))
            except Exception:
                try:
                    df = con.execute(f"SELECT * FROM {quoted_name} LIMIT 0").df()
                    for c in df.columns:
                        cols.append(TableColumn(name=str(c), dtype="unknown"))
                except Exception:
                    pass
        return Table(name=table_name, columns=cols, pks=[], fks=[])

    def get_schemas(self):
        return self.get_tables()

    def prompt_schema(self):
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        try:
            with self.connect() as con:
                con.execute("SELECT 1")
                if self.database:
                    # For database files, try to list tables
                    tables = con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main' LIMIT 1").fetchall()
                    return {"success": True, "message": f"DuckDB database connected: {self.database}"}
                elif self.uri_patterns:
                    # Try reading first pattern minimally if present
                    view_names = con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main' AND table_type='VIEW' ORDER BY table_name LIMIT 1").fetchall()
                    if view_names:
                        vn = view_names[0][0]
                        quoted_vn = self._quote_identifier(vn)
                        con.execute(f"SELECT * FROM {quoted_vn} LIMIT 1")
                return {"success": True, "message": "DuckDB connected and views ready"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    @property
    def description(self):
        if self.database:
            return f"DuckDB database: {self.database}"
        sample = ", ".join(self.uri_patterns[:2])
        if len(self.uri_patterns) > 2:
            sample += ", ..."
        return f"DuckDB over files. URIs: {sample}"

# Compatibility alias for dynamic resolver expecting 'DuckdbClient'
DuckdbClient = DuckDBClient


