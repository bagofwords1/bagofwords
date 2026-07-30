from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from functools import cached_property
from typing import Generator, List, Optional

import pandas as pd

from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import ProgressCallback, make_reporter


class SqliteClient(DataSourceClient):
    """Lightweight SQLite client primarily intended for dev/test workflows."""

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
        return f"sqlite:///{self.database}"

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
        with get_engine(self.sqlite_uri).connect() as conn:
            yield conn

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(self.database, check_same_thread=False)
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
                    # PRAGMA does not accept bound parameters for object names,
                    # so validate the name shape before interpolating to guard
                    # against any odd values from sqlite_master.
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
                        continue
                    cursor.execute(f'PRAGMA table_info("{table_name}")')
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
                return tables
        except Exception as exc:
            print(f"Error retrieving tables: {exc}")
            return []

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

