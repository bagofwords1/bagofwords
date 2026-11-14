from app.data_sources.clients.progress import discovery_progress, discovery_items, IndexingCancelled
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients._duckdb_lockdown import lock_down

import duckdb
import os
import pandas as pd
import re
from contextlib import contextmanager
from typing import Generator, List
from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
import urllib.parse

# Characters that make a URI a glob pattern to DuckDB. In an http(s) URL `?`
# starts the query string and `[` is an ordinary character: only `*` globs.
_GLOB_CHAR = re.compile(r"[*?\[]")
_HTTP_GLOB_CHAR = re.compile(r"\*")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_ROOT_GLOB = ("A URI of this connection is a glob or a directory at the root of the local file system, "
              "directly after its scheme, or with a .. segment after its scheme. "
              "Name the directory its files are in.")
_RELATIVE_GLOB = ("A URI of this connection is a glob or a directory relative to the working directory that "
                  "names no directory under it. Name the directory its files are in.")


def _expanded(uri: str) -> str:
    """A local path or file URI with its file prefix removed and its home
    directory expanded, as DuckDB does both: `file://localhost/` and
    `file:/` keep their '/', `file://` goes (DuckDB matches these prefixes
    in lower case only); only a bare `~` or a leading `~/` is the home
    directory."""
    if uri.startswith("file://localhost/"):
        uri = uri[len("file://localhost"):]
    elif uri.startswith("file://"):
        uri = uri[len("file://"):]
    elif uri.startswith("file:/"):
        uri = uri[len("file:"):]
    if uri == "~" or uri.startswith("~/"):
        uri = os.path.expanduser(uri)
    return uri


def _local_path(uri: str) -> str:
    """A local path or file URI as DuckDB resolves it (`_expanded`), made
    absolute with `..` resolved, one leading '/'."""
    return "/" + os.path.abspath(_expanded(uri)).lstrip("/")


def _local_directory(directory: str) -> str:
    """`directory` (a local path or file URI) resolved as DuckDB resolves
    it (`_local_path`), ending in '/'. Raises ValueError for the root and
    for a relative directory that resolves to the working directory or
    one above it."""
    resolved = _local_path(directory)
    if not _expanded(directory).startswith("/"):
        if os.path.commonpath([resolved, os.getcwd()]) == resolved:
            raise ValueError(_RELATIVE_GLOB)
    if resolved == "/":
        raise ValueError(_ROOT_GLOB)
    return os.path.join(resolved, "")


class DuckDBClient(DataSourceClient):

    # Rendered into codegen prompts (<connection_clients>) so generated queries
    # express time windows with the engine's own relative date functions instead
    # of literal dates that go stale when saved code is re-executed.
    relative_date_hint = "Relative dates (DuckDB): current_date, current_date - INTERVAL 7 DAY, date_trunc('month', current_date); the clock is the app server's local time."

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
        except IndexingCancelled:
            raise
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

    def view_names(self) -> List[str]:
        """The name of the view `connect()` registers for each URI, in URI
        order: the tables agent SQL may read over URIs. Pure: opens nothing."""
        names: List[str] = []
        used: set[str] = set()
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
            names.append(self._safe_view_name(candidate, used))
        return names

    def _create_views(self, con: duckdb.DuckDBPyConnection) -> List[str]:
        created: List[str] = []
        for pattern, view in zip(self.uri_patterns, self.view_names()):
            normalized = self._normalize_uri(pattern)
            lower = normalized.lower()
            if lower.endswith(".parquet") or ".parquet" in lower:
                con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet({self._sql_literal(normalized)})")
            else:
                # default to CSV auto
                con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_csv_auto({self._sql_literal(normalized)})")
            created.append(view)
        return created

    def _lockdown_targets(self) -> tuple[List[str], List[str]]:
        """(the exact paths the views read; the directories they may read
        under). A URI without a glob character is an exact path, and a
        non-http(s) one ending in '/', or a local one that is a directory
        (DuckDB reads every file under either), is both an exact path and a
        directory. A glob URI's directory is the part up to the last '/'
        before its first glob character.

        A local directory (a path or file URI) is resolved as DuckDB
        resolves it (`_local_path`): file prefix stripped, `~` expanded,
        `..` and repeated slashes resolved, made absolute. Raises ValueError
        when it resolves to the root or, relative, to the working directory
        or one above it; and when another scheme's directory is empty after the
        scheme or holds a '..' segment. A bucket or a host on its own is
        a directory like any other."""
        paths: List[str] = []
        directories: List[str] = []
        for pattern in self.uri_patterns:
            uri = self._normalize_uri(pattern)
            scheme = _SCHEME.match(uri)
            local = scheme is None or uri.startswith("file:")
            http = uri.lower().startswith(("http://", "https://"))
            glob = (_HTTP_GLOB_CHAR if http else _GLOB_CHAR).search(uri)
            if glob is None and (http or not uri.endswith("/")):
                paths.append(uri)
                resolved = _local_path(uri) if local else uri
                if resolved != uri:
                    paths.append(resolved)
                if not (local and os.path.isdir(resolved)):
                    continue
                # A directory written without its '/': DuckDB reads every
                # file under it, as it does for the '/' form.
                directory = _local_directory(uri)
                paths.append(directory)
                directories.append(directory)
                continue
            directory = uri if glob is None else uri[:uri.rfind("/", 0, glob.start()) + 1]
            if local:
                directory = _local_directory(directory)
            else:
                after = directory[scheme.end():]
                if not after.strip("/") or ".." in after.split("/"):
                    raise ValueError(_ROOT_GLOB)
            if glob is None:
                paths.append(directory)
            directories.append(directory)
        return paths, directories

    def _find_local_duckdb_file(self) -> str | None:
        """Check if any URI pattern is a local .duckdb or .db file.

        Supports both direct paths (/data/my.duckdb) and file:// URIs (file:///data/my.duckdb).
        Returns the normalized path (strips file:// prefix if present).
        """
        for pattern in self.uri_patterns:
            lower = pattern.lower()
            if lower.endswith('.duckdb') or lower.endswith('.db'):
                # Strip file:// prefix if present
                if lower.startswith('file://'):
                    return pattern[7:]  # Remove 'file://' prefix
                return pattern
        return None

    @contextmanager
    def connect(self) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        con: duckdb.DuckDBPyConnection | None = None
        try:
            local_db = self._find_local_duckdb_file()

            if self.database:
                # Open local .duckdb file directly (read-only for safety)
                con = duckdb.connect(database=self.database, read_only=True)
                lock_down(con)
            elif local_db:
                # Direct connection to local .duckdb file specified in URIs
                con = duckdb.connect(database=local_db, read_only=True)
                lock_down(con)
            else:
                # What the connection will be locked to, worked out before
                # anything opens: a glob at a root is refused here.
                paths, directories = self._lockdown_targets()
                # In-memory database with views from URI patterns
                con = duckdb.connect(database=":memory:")
                self._configure_httpfs(con)
                self._create_views(con)
                # The views read their URIs lazily: from here on the
                # connection may read those and nothing else.
                lock_down(con, allowed_paths=paths, allowed_directories=directories)
            yield con
        except IndexingCancelled:
            raise
        except Exception as e:
            raise RuntimeError(f"Error while connecting to DuckDB: {e}")
        finally:
            try:
                if con is not None:
                    con.close()
            except IndexingCancelled:
                raise
            except Exception:
                pass

    def execute_query(self, sql: str) -> pd.DataFrame:
        try:
            with self.connect() as con:
                res = con.execute(sql)
                return res.df()
        except Exception as e:
            raise

    def _is_direct_db_connection(self) -> bool:
        """Check if we're connecting directly to a database file."""
        return bool(self.database or self._find_local_duckdb_file())

    def get_tables(self) -> List[Table]:
        tables: List[Table] = []
        with self.connect() as con:
            # For database files, list actual tables; for URI mode, list views
            if self._is_direct_db_connection():
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
            for (name,) in discovery_items(rows, 'tables', label=lambda row: row[0]):
                cols = []
                try:
                    desc = con.execute(f"DESCRIBE {name}").fetchall()
                    for d in desc:
                        # DuckDB DESCRIBE columns: column_name, column_type, null, key, default, extra
                        col_name = d[0]
                        col_type = d[1]
                        cols.append(TableColumn(name=col_name, dtype=str(col_type)))
                except IndexingCancelled:
                    raise
                except Exception:
                    # Fallback: zero-row scan
                    try:
                        df = con.execute(f"SELECT * FROM {name} LIMIT 0").df()
                        for c in df.columns:
                            cols.append(TableColumn(name=str(c), dtype="unknown"))
                    except IndexingCancelled:
                        raise
                    except Exception:
                        self.mark_discovery_incomplete("duckdb catalog")
                        pass
                tables.append(Table(name=name, columns=cols, pks=[], fks=[]))
        return tables

    def get_schema(self, table_name: str) -> Table:
        cols: List[TableColumn] = []
        with self.connect() as con:
            try:
                desc = con.execute(f"DESCRIBE {table_name}").fetchall()
                for d in desc:
                    col_name = d[0]
                    col_type = d[1]
                    cols.append(TableColumn(name=col_name, dtype=str(col_type)))
            except Exception:
                try:
                    df = con.execute(f"SELECT * FROM {table_name} LIMIT 0").df()
                    for c in df.columns:
                        cols.append(TableColumn(name=str(c), dtype="unknown"))
                except Exception:
                    pass
        return Table(name=table_name, columns=cols, pks=[], fks=[])

    @discovery_progress
    def get_schemas(self, progress_callback=None):
        return self.get_tables()

    def prompt_schema(self):
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        try:
            with self.connect() as con:
                con.execute("SELECT 1")
                local_db = self._find_local_duckdb_file()
                if self.database:
                    # For database files, try to list tables
                    con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main' LIMIT 1").fetchall()
                    return {"success": True, "message": f"DuckDB database connected: {self.database}"}
                elif local_db:
                    # Local .duckdb file specified in URIs
                    con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main' LIMIT 1").fetchall()
                    return {"success": True, "message": f"DuckDB database connected: {local_db}"}
                elif self.uri_patterns:
                    # Try reading first pattern minimally if present
                    view_names = con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main' AND table_type='VIEW' ORDER BY table_name LIMIT 1").fetchall()
                    if view_names:
                        vn = view_names[0][0]
                        con.execute(f"SELECT * FROM {vn} LIMIT 1")
                return {"success": True, "message": "DuckDB connected and views ready"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    @property
    def description(self):
        system_prompt = """
        You can call the execute_query method to run SQL queries using DuckDB syntax.

        The below are examples for how to use the execute_query method. Note that the actual SQL will vary based on the schema.
        Notice only the SQL syntax and instructions on how to use the execute_query method, not the actual SQL queries.

        ```python
        df = client.execute_query("SELECT * FROM sales LIMIT 10")
        ```
        or:
        ```python
        df = client.execute_query("SELECT product, SUM(amount) AS total FROM sales GROUP BY ALL")
        ```

        """
        local_db = self._find_local_duckdb_file()
        if self.database:
            description = f"DuckDB database: {self.database}\n\n"
        elif local_db:
            description = f"DuckDB database: {local_db}\n\n"
        else:
            sample = ", ".join(self.uri_patterns[:2])
            if len(self.uri_patterns) > 2:
                sample += ", ..."
            description = f"DuckDB over files. URIs: {sample}\n\n"
        description += system_prompt
        return description

# Compatibility alias for dynamic resolver expecting 'DuckdbClient'
DuckdbClient = DuckDBClient


