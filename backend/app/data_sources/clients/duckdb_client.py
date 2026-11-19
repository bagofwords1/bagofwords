from app.data_sources.clients.progress import discovery_progress, discovery_items, IndexingCancelled
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients import _duckdb_lockdown as lockdown
from app.data_sources.clients._duckdb_lockdown import _list_literal
from app.ai.code_execution.sql_guard import ReadOnlyViolation

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
_FILE_HOST = ("A URI of this connection names a host after file://. "
              "Write a local file as file:///path or file://localhost/path.")
_NO_WORKING_DIRECTORY = ("A URI of this connection is relative, and the working directory it is relative to "
                         "no longer exists. Name its files from the root.")


def _spelled_absolute(uri: str) -> bool:
    """Whether a local path or file URI names its file from the root as
    written: '/...', 'file:/...', 'file:///...' or 'file://localhost/...'.
    Every other local URI, a `~` one included, is resolved against
    something else."""
    return uri.startswith(("/", "file:/"))


def _working_directory() -> str:
    try:
        return os.path.realpath(os.getcwd())
    except FileNotFoundError:
        raise ValueError(_NO_WORKING_DIRECTORY) from None


def _read_back(con: duckdb.DuckDBPyConnection, setting: str, uri: str) -> str:
    """`uri` as DuckDB itself resolves it in `setting` (allowed_paths or
    allowed_directories): set on its own on `con`, then read back. The
    setting is left empty."""
    con.execute(f"SET {setting} = {_list_literal([uri])}")
    entries = con.execute(f"SELECT current_setting('{setting}')").fetchone()[0]
    con.execute(f"SET {setting} = []")
    if len(entries) != 1:
        raise ValueError(f"DuckDB resolved a URI of this connection to {len(entries)} entries in {setting}.")
    return entries[0]


def _resolved_directory(con: duckdb.DuckDBPyConnection, directory: str, cwd: str | None) -> str:
    """`directory` (a local path or file URI) as DuckDB resolves it,
    ending in '/'. Raises ValueError when that is the root, or, for a
    directory not spelled absolute (`cwd` given), the working directory
    `cwd` or one above it."""
    resolved = _read_back(con, "allowed_directories", directory)
    if not resolved.strip("/"):
        raise ValueError(_ROOT_GLOB)
    if cwd is not None and os.path.commonpath([resolved, cwd]) == resolved.rstrip("/"):
        raise ValueError(_RELATIVE_GLOB)
    return resolved


class DuckDBClient(DataSourceClient):

    # '' is the only quote escape (sqlglot duckdb).
    sql_literal_style = "standard"

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

    @staticmethod
    def _quoted(name: str) -> str:
        """An identifier as DuckDB reads it whatever it contains (a view named
        after a file can start with a digit; a table can hold a space)."""
        return '"' + str(name).replace('"', '""') + '"'

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
            if lockdown.unrestricted():
                con.execute(f"SET s3_access_key_id={self._sql_literal(self.access_key)};")
                con.execute(f"SET s3_secret_access_key={self._sql_literal(self.secret_key)};")
                if self.region:
                    con.execute(f"SET s3_region={self._sql_literal(self.region)};")
                if self.session_token:
                    con.execute(f"SET s3_session_token={self._sql_literal(self.session_token)};")
            else:
                self._create_object_store_secrets(con)

        # GCP
        if self.service_account_json:
            # DuckDB accepts JSON credentials via setting
            con.execute(f"SET gcs_credentials={self._sql_literal(self.service_account_json)};")

        # Azure
        if self.connection_string:
            # DuckDB supports Azure via connection string (SAS/account key)
            if lockdown.unrestricted():
                con.execute(f"SET azure_storage_connection_string={self._sql_literal(self.connection_string)};")
            else:
                con.execute(
                    "CREATE OR REPLACE SECRET bow_azure (TYPE azure, "
                    f"CONNECTION_STRING {self._sql_literal(self.connection_string)})"
                )

    def _create_object_store_secrets(self, con: duckdb.DuckDBPyConnection) -> None:
        """The access key as session secrets instead of settings.

        A setting can be read back by any query (`current_setting`,
        `duckdb_settings()`); a secret is used to sign requests and is shown
        redacted. Requests are signed the same way either way. The URL style
        and TLS settings above still apply. The key covers s3:// and, as the
        settings did, gs:// (HMAC keys through the S3-compatible API).
        """
        # A secret without a region signs with an empty one; the settings
        # path used the session default.
        region = self.region or con.execute("SELECT current_setting('s3_region')").fetchone()[0] or "us-east-1"
        fields = [
            f"KEY_ID {self._sql_literal(self.access_key)}",
            f"SECRET {self._sql_literal(self.secret_key)}",
        ]
        if self.session_token:
            fields.append(f"SESSION_TOKEN {self._sql_literal(self.session_token)}")
        s3_fields = fields + [f"REGION {self._sql_literal(region)}"]
        con.execute(f"CREATE OR REPLACE SECRET bow_s3 (TYPE s3, {', '.join(s3_fields)})")
        con.execute(f"CREATE OR REPLACE SECRET bow_gcs (TYPE gcs, {', '.join(fields)})")

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
                con.execute(f"CREATE OR REPLACE VIEW {self._quoted(view)} AS SELECT * FROM read_parquet({self._sql_literal(normalized)})")
            else:
                # default to CSV auto
                con.execute(f"CREATE OR REPLACE VIEW {self._quoted(view)} AS SELECT * FROM read_csv_auto({self._sql_literal(normalized)})")
            created.append(view)
        return created

    def _lockdown_targets(self, con: duckdb.DuckDBPyConnection) -> tuple[List[str], List[str]]:
        """(the exact paths the views read; the directories they may read
        under), for locking `con`, which has no views yet. A URI without a
        glob character is an exact path, and a non-http(s) one ending in
        '/', or a local one that is a directory (DuckDB reads every file
        under either), is both an exact path and a directory. A glob URI's
        directory is the part up to the last '/' before its first glob
        character.

        A local path or file URI is taken in the form DuckDB itself
        resolves it to (set on `con`, then read back: links, `~`, `..`,
        file prefixes and the working directory all resolved). Raises
        ValueError when a local directory resolves to the root or, not
        spelled absolute, to the working directory or one above it; when
        a file URI names a host; and when another scheme's directory is
        empty after the scheme or holds a '..' segment. A bucket or a host
        on its own is a directory like any other."""
        paths: List[str] = []
        directories: List[str] = []
        for pattern in self.uri_patterns:
            uri = self._normalize_uri(pattern)
            if uri.startswith("file://") and not uri.startswith(("file:///", "file://localhost/")):
                raise ValueError(_FILE_HOST)
            scheme = _SCHEME.match(uri)
            local = scheme is None or uri.startswith("file:")
            cwd = None if not local or _spelled_absolute(uri) else _working_directory()
            http = uri.lower().startswith(("http://", "https://"))
            glob = (_HTTP_GLOB_CHAR if http else _GLOB_CHAR).search(uri)
            if glob is None and (http or not uri.endswith("/")):
                if not local:
                    paths.append(uri)
                    continue
                path = _read_back(con, "allowed_paths", uri)
                paths.append(path)
                if not os.path.isdir(path):
                    continue
                # A directory written without its '/': DuckDB reads every
                # file under it, as it does for the '/' form.
                directory = _resolved_directory(con, uri, cwd)
                paths.append(directory)
                directories.append(directory)
                continue
            directory = uri if glob is None else uri[:uri.rfind("/", 0, glob.start()) + 1]
            if local:
                directory = _resolved_directory(con, directory, cwd)
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
            db_file = self.database or local_db

            if db_file:
                # Open the local .duckdb file directly, read-only.
                if lockdown.unrestricted():
                    con = duckdb.connect(database=db_file, read_only=True)
                else:
                    # Besides the file: its own folder, where the files its
                    # views read usually sit, and any other URI listed on the
                    # connection, as the path policy (organization roots
                    # included) accepts them, before the file is opened.
                    others = [p for p in self.uri_patterns if lockdown.local_path_of(p) != db_file and p != db_file]
                    paths, directories = lockdown.database_file_sources(
                        db_file, others, lockdown.organization_of(self))
                    con = duckdb.connect(database=db_file, read_only=True)
                    lockdown.lock_connection(con, paths=paths, directories=directories, database_path=db_file)
            elif lockdown.unrestricted():
                # In-memory database with views from URI patterns, unlocked.
                con = duckdb.connect(database=":memory:")
                self._configure_httpfs(con)
                self._create_views(con)
            else:
                # In-memory database with views from URI patterns.
                con = duckdb.connect(database=":memory:")
                # Secrets come from this connection's settings only, never
                # from files on disk, which the lock would refuse anyway.
                # Set first: DuckDB refuses the change once anything (an
                # extension download included) has used its secrets.
                con.execute("SET allow_persistent_secrets = false")
                # Extensions and their settings: none can change once locked.
                self._configure_httpfs(con)
                # What the connection is locked to, in the form DuckDB
                # resolves each URI to; a glob at a root is refused here,
                # before anything is read.
                paths, directories = self._lockdown_targets(con)
                # The path policy, before anything is read.
                self._allowed_sources()
                lockdown.lock_connection(con, paths=paths, directories=directories)
                # Views are created on the locked connection: one over
                # anything else fails at once instead of reading it.
                self._create_views(con)
            yield con
        except (IndexingCancelled, ReadOnlyViolation):
            # A query refused on the session keeps its type.
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

    def _allowed_sources(self) -> tuple[list[str], list[str]]:
        """Exact files/URLs and directory/URL prefixes this connection reads."""
        if lockdown.unrestricted():
            return [], []
        return lockdown.sources_for_patterns(
            (self._normalize_uri(p) for p in self.uri_patterns), lockdown.organization_of(self))

    def execute_query(self, sql: str) -> pd.DataFrame:
        lockdown.check_user_sql(sql, deferred=True)
        try:
            with self.connect() as con:
                lockdown.check_session_sql(con, sql)
                res = con.execute(sql)
                return res.df()
        except Exception as e:
            explained = lockdown.explain_access_error(e)
            if explained:
                raise RuntimeError(explained) from e
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
                    desc = con.execute(f"DESCRIBE {self._quoted(name)}").fetchall()
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
                        df = con.execute(f"SELECT * FROM {self._quoted(name)} LIMIT 0").df()
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
                desc = con.execute(f"DESCRIBE {self._quoted(table_name)}").fetchall()
                for d in desc:
                    col_name = d[0]
                    col_type = d[1]
                    cols.append(TableColumn(name=col_name, dtype=str(col_type)))
            except Exception:
                try:
                    df = con.execute(f"SELECT * FROM {self._quoted(table_name)} LIMIT 0").df()
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
                        con.execute(f"SELECT * FROM {self._quoted(vn)} LIMIT 1")
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


