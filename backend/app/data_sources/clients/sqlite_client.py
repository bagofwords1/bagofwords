from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Generator, List, Optional

import pandas as pd

from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import ProgressCallback, make_reporter


class SqliteClient(DataSourceClient):
    """Lightweight SQLite client primarily intended for dev/test workflows."""

    def __init__(self, database: str = ":memory:"):
        self.database = database

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
                    cursor.execute(f"PRAGMA table_info('{table_name}')")
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

