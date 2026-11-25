from __future__ import annotations
from app.data_sources.clients.progress import discovery_progress

import logging
import os
import sqlite3
import urllib.parse
from contextlib import contextmanager
from functools import cached_property
from typing import Generator, List, Optional

import pandas as pd

from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.data_sources.clients.progress import IndexingCancelled
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import ProgressCallback, make_reporter
from app.data_sources.fk_reflection import attach_foreign_keys

logger = logging.getLogger(__name__)


# PRAGMAs that take an argument and only report (the argument names an object).
_READ_PRAGMAS = frozenset({
    "table_info", "table_xinfo", "table_list", "index_list", "index_info", "index_xinfo",
    "foreign_key_list", "foreign_key_check", "integrity_check", "quick_check",
})


def _authorize_read(action, arg1, arg2, _db, _source):
    """Engine-side backstop for the read-only open: a read-only handle can
    still attach a second database read-write, and `PRAGMA name(value)` sets
    `name`."""
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and arg2 is not None and (arg1 or "").lower() not in _READ_PRAGMAS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class SqliteClient(DataSourceClient):
    """Lightweight SQLite client primarily intended for dev/test workflows."""

    # SQLite: '' only (sqlglot sqlite).
    sql_literal_style = "standard"

    # Rendered into codegen prompts (<connection_clients>) so generated queries
    # express time windows with the engine's own relative date functions instead
    # of literal dates that go stale when saved code is re-executed.
    relative_date_hint = "Relative dates (SQLite): date('now'), date('now', '-7 day'), date('now', 'start of month'); 'now' is UTC — add the 'localtime' modifier for server-local."

    @property
    def EXTRACTION_DIALECT(self):
        """"sqlite" only when there is a real file to open a second handle to.

        An in-memory database is private to the connection that created it, so
        a second handle opens a *different*, empty one — extraction would
        silently materialize nothing. Declaring no dialect is what keeps it out
        of the accelerable set, so the option is never offered rather than
        failing at refresh time.
        """
        return "sqlite" if self.sqlite_uri else ""

    def __init__(self, database: str = ":memory:"):
        self.database = database
        if database and database != ":memory:":
            # A generated demo dataset lives under uploads/; with object
            # storage another pod may hold it. No-op for any other path.
            from app.services import storage
            if storage.enabled() and storage.key_for(database):
                storage.ensure_local(database, fresh=False)

    @cached_property
    def sqlite_uri(self) -> str:
        """SQLAlchemy URI for the same file this client opens with raw sqlite3.

        The client's own `connect()` yields a `sqlite3.Connection` because its
        catalog reads use PRAGMA and `row_factory`. Custom-query extraction
        needs a SQLAlchemy connection instead (server-side cursor, batched
        fetch), so it addresses the database through this URI. Both point at
        one file; SQLite's own locking is what keeps them honest.
        """
        if not self.database or self.database == ":memory:":
            # An in-memory database is private to the connection that made it,
            # so a second handle would open a *different*, empty database.
            return ""
        from app.data_sources.clients._duckdb_lockdown import unrestricted

        if unrestricted():
            return f"sqlite:///{self.database}"
        # Extraction and FK reflection only read: same read-only open as connect().
        return f"sqlite:///{self._read_only_file_uri()}&uri=true"

    @contextmanager
    def extraction_connect(self):
        """A SQLAlchemy Connection over the same file, for extraction.

        `connect()` yields a raw `sqlite3.Connection` because the catalog reads
        use PRAGMA and `row_factory`; extraction needs SQLAlchemy for its
        server-side cursor. Both address one file, and SQLite's own locking is
        what keeps them honest.
        """
        from app.data_sources.engine_pool import get_engine

        if not self.sqlite_uri:
            raise RuntimeError(
                "An in-memory SQLite database cannot be materialized: a second "
                "handle opens a different, empty database."
            )
        self._check_file()
        with get_engine(self.sqlite_uri).connect() as conn:
            yield conn

    def check_config_paths(self) -> None:
        """Path policy for the configured database file. Raises ValueError."""
        if self.database and self.database != ":memory:":
            from app.data_sources.clients._duckdb_lockdown import check_local_path

            # The owning organization, when whoever built the client knew it.
            check_local_path(self.database, organization_id=getattr(self, "_bow_organization_id", None))

    def _read_only_file_uri(self) -> str:
        return "file:" + urllib.parse.quote(os.path.abspath(self.database)) + "?mode=ro"

    def _check_file(self) -> None:
        """Path policy, then a clear error for a file that is not there: a
        read-only open does not create one, and SQLite's own message
        ("unable to open database file") does not say why."""
        from app.data_sources.clients._duckdb_lockdown import unrestricted

        if unrestricted():
            return
        self.check_config_paths()
        if not os.path.isfile(self.database):
            raise FileNotFoundError(f"database file not found: {self.database}")

    def _open(self) -> sqlite3.Connection:
        from app.data_sources.clients._duckdb_lockdown import unrestricted

        if unrestricted():
            return sqlite3.connect(self.database, check_same_thread=False)
        if not self.database or self.database == ":memory:":
            conn = sqlite3.connect(self.database, check_same_thread=False)
            conn.set_authorizer(_authorize_read)
            return conn
        self._check_file()
        # A data source is only ever read: open the file read-only, so no
        # statement can change it, whatever the SQL guard let through.
        conn = sqlite3.connect(self._read_only_file_uri(), uri=True, check_same_thread=False)
        conn.set_authorizer(_authorize_read)
        return conn

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn: sqlite3.Connection | None = None
        try:
            conn = self._open()
            conn.row_factory = sqlite3.Row
            yield conn
        except Exception as exc:
            raise RuntimeError(f"{exc}") from exc
        finally:
            if conn is not None:
                conn.close()

    def execute_query(self, sql: str) -> pd.DataFrame:
        try:
            with self.connect() as conn:
                df = pd.read_sql_query(sql, conn)
            return df
        except Exception as exc:
            print(f"Error executing SQL: {exc}")
            raise

    def get_tables(self, progress_callback: Optional[ProgressCallback] = None) -> List[Table]:
        reporter = make_reporter(progress_callback)
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
                names = [row[0] for row in cursor.fetchall()]
                reporter.phase("tables", total=len(names))
                tables: List[Table] = []
                for table_name in names:
                    reporter.item(table_name)
                    # The table-valued form takes the name as a bound
                    # parameter, so any name sqlite_master holds (spaces,
                    # hyphens, quotes, non-ASCII) is read as data, never SQL.
                    cursor.execute(
                        "SELECT name, type FROM pragma_table_info(?)", (table_name,)
                    )
                    columns = [
                        TableColumn(name=row["name"], dtype=row["type"] or "unknown")
                        for row in cursor.fetchall()
                    ]
                    tables.append(
                        Table(
                            name=table_name,
                            columns=columns,
                            pks=[],
                            fks=[],
                            metadata_json={"database": self.database},
                        )
                    )
                reporter.done()
                self._attach_foreign_keys(tables)
                return tables
        except IndexingCancelled:
            raise
        except Exception as exc:
            self.mark_discovery_incomplete("sqlite catalog")
            print(f"Error retrieving tables: {exc}")
            return []

    def _attach_foreign_keys(self, tables: list[Table]) -> None:
        """Populate `fks` on the tables just collected.

        The catalog reads above use raw sqlite3 (PRAGMA, `row_factory`), which
        `sqlalchemy.inspect` cannot drive, so reflection addresses the same file
        through the SQLAlchemy handle `extraction_connect` already opens for
        extraction. SQLite's own locking is what keeps the two honest.

        An in-memory database is the one case that cannot work: it is private
        to the connection that created it, so a second handle would open a
        different, empty database and report no constraints at all. Better to
        skip than to state that confidently.

        Names are bare here, so both the reference string and the lookup key
        drop the schema.
        """
        if not self.sqlite_uri:
            return
        try:
            with self.extraction_connect() as conn:
                attach_foreign_keys(
                    conn,
                    {t.name: t for t in tables},
                    None,
                    lambda schema, table: table,
                    key_fn=lambda schema, table: table,
                )
        except Exception:
            logger.debug("SQLite FK reflection failed; continuing", exc_info=True)

    @discovery_progress
    def get_schemas(self, progress_callback: Optional[ProgressCallback] = None):
        return self.get_tables(progress_callback=progress_callback)

    def get_schema(self, table_id: str):
        raise NotImplementedError("get_schema() is obsolete. Use get_tables() instead.")

    def prompt_schema(self):
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        import os as _os, time as _time

        t0 = _time.perf_counter()
        try:
            with self.connect() as conn:
                conn.execute("SELECT 1")
                sqlite_ver = conn.execute("SELECT sqlite_version()").fetchone()[0]
                table_count = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()[0]
            details = {
                "server_version": f"sqlite {sqlite_ver}",
                "database": self.database,
                "database_bytes": (_os.path.getsize(self.database) if self.database != ":memory:" and _os.path.exists(self.database) else None),
                "table_count": table_count,
            }
            return {
                "success": True,
                "message": f"Successfully connected to SQLite database {self.database}",
                "timings": {"connect_ms": round((_time.perf_counter() - t0) * 1000, 1)},
                "details": details,
            }
        except Exception as exc:
            return {
                "success": False,
                "message": str(exc),
                "timings": {"connect_ms": round((_time.perf_counter() - t0) * 1000, 1)},
                "details": {"database": self.database},
            }

    @property
    def description(self):
        system_prompt = """
        You can call the execute_query method to run SQL queries.

        The below are examples for how to use the execute_query method. Note that the actual SQL will vary based on the schema.
        Notice only the SQL syntax and instructions on how to use the execute_query method, not the actual SQL queries.

        ```python
        df = client.execute_query("SELECT * FROM users")
        ```
        or:
        ```python
        df = client.execute_query("SELECT * FROM users WHERE age > 30")
        ```
        """
        description = f"SQLite database at {self.database}\n\n"
        description += system_prompt
        return description

