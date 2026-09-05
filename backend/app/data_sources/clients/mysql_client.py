from app.data_sources.clients.base import DataSourceClient

import pandas as pd
import sqlalchemy

from app.data_sources.engine_pool import get_engine
from app.data_sources.query_cancellation import track
from sqlalchemy import text
from contextlib import contextmanager
from typing import List, Generator, Optional
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from app.data_sources.fk_reflection import attach_foreign_keys
from functools import cached_property
from urllib.parse import quote_plus


class MysqlClient(DataSourceClient):

    # Rendered into codegen prompts (<connection_clients>) so generated queries
    # express time windows with the engine's own relative date functions instead
    # of literal dates that go stale when saved code is re-executed.
    relative_date_hint = "Relative dates (MySQL): CURDATE(), CURDATE() - INTERVAL 7 DAY, DATE_FORMAT(CURDATE(), '%Y-%m-01') for month start; the clock is the DB server's."

    def __init__(self, host, port, database, user: Optional[str] = None, password: Optional[str] = None):
        self.host = host
        self.port = port
        self.database = database
        if password not in (None, "") and user in (None, ""):
            raise ValueError("A user must be provided when supplying a password.")
        self.user = user
        self.password = password

    @cached_property
    def mysql_uri(self):
        auth_part = ""
        if self.user is not None:
            auth_part = quote_plus(self.user)
            if self.password is not None:
                auth_part += f":{quote_plus(self.password)}"
            auth_part += "@"
        uri = f"mysql+pymysql://{auth_part}{self.host}:{self.port}/{self.database}"
        return uri

    @contextmanager
    def connect(self) -> Generator[sqlalchemy.engine.base.Connection, None, None]:
        """Yield a connection to a MySQL db."""
        conn = None
        try:
            engine = get_engine(self.mysql_uri)
            conn = engine.connect()
        except Exception as e:
            if conn is not None:
                conn.close()
            raise RuntimeError(f"{e}")
        # The yield is deliberately OUTSIDE the try/except above. With it
        # inside, this contextmanager caught whatever the *caller* raised in
        # its `with client.connect()` body and re-raised it as a bare
        # RuntimeError, erasing the type: an extraction abort came back
        # indistinguishable from a connection failure. The except clause is
        # meant to wrap connect-time errors, and now only does.
        try:
            with track(self, conn):
                yield conn
        finally:
            conn.close()
        # NB: no engine.dispose() — the engine is pooled and shared
        # (engine_pool). conn.close() above returns the connection.

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute SQL statement and return the result as a DataFrame."""
        try:
            with self.connect() as conn:
                df = pd.read_sql(text(sql), conn)
            return df
        except Exception as e:
            print(f"Error executing SQL: {e}")
            raise

    def get_tables(self) -> List[Table]:
        """Get tables with graceful fallback if enriched query fails."""
        try:
            return self._get_tables_enriched()
        except Exception:
            return self._get_tables_basic()

    def _get_tables_enriched(self) -> List[Table]:
        """Get tables with column/table comments. May fail on some MySQL versions."""
        with self.connect() as conn:
            sql = """
                SELECT
                    c.table_name,
                    c.column_name,
                    c.data_type,
                    c.column_comment,
                    t.table_comment
                FROM information_schema.columns c
                LEFT JOIN information_schema.tables t
                    ON c.table_schema = t.table_schema AND c.table_name = t.table_name
                WHERE c.table_schema = :database
                ORDER BY c.table_name, c.ordinal_position
            """
            result = conn.execute(text(sql), {'database': self.database}).fetchall()

            tables = {}
            for row in result:
                table_name, column_name, data_type, col_comment, tbl_comment = row

                if table_name not in tables:
                    tables[table_name] = Table(
                        name=table_name,
                        description=tbl_comment if tbl_comment else None,
                        columns=[],
                        pks=None,
                        fks=None
                    )
                tables[table_name].columns.append(TableColumn(
                    name=column_name,
                    dtype=data_type,
                    description=col_comment if col_comment else None
                ))
            self._attach_foreign_keys(conn, tables)
            return list(tables.values())

    def _get_tables_basic(self) -> List[Table]:
        """Get tables without comments (original query - always works)."""
        try:
            with self.connect() as conn:
                sql = """
                    SELECT table_name, column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = :database
                    ORDER BY table_name, ordinal_position
                """
                result = conn.execute(
                    text(sql), {'database': self.database}).fetchall()

                tables = {}
                for row in result:
                    table_name, column_name, data_type = row

                    if table_name not in tables:
                        tables[table_name] = Table(
                            name=table_name, columns=[], pks=None, fks=None)
                    tables[table_name].columns.append(
                        TableColumn(name=column_name, dtype=data_type))
                self._attach_foreign_keys(conn, tables)
            return list(tables.values())
        except Exception as e:
            print(f"Error retrieving tables: {e}")
            return []

    def get_schema(self, table_id: str) -> Table:
        """This method is now obsolete. Please use get_tables() instead."""
        raise NotImplementedError(
            "get_schema() is obsolete. Use get_tables() instead.")

    def _attach_foreign_keys(self, conn, tables) -> None:
        """Populate `fks` on the tables just collected.

        This client addresses a single database and names tables bare, so both
        the reference string and the dict key drop the schema. Passing the
        default tuple key here would match nothing and silently yield no edges.
        """
        attach_foreign_keys(
            conn,
            tables,
            None,
            lambda schema, table: table,
            key_fn=lambda schema, table: table,
        )

    def get_schemas(self):
        """Get schemas for all tables in the specified database."""
        return self.get_tables()

    def prompt_schema(self):
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        """Test connection to MySQL and return status information."""
        try:
            with self.connect() as conn:
                conn.execute(text("SELECT 1"))
                return {
                    "success": True,
                    "message": "Successfully connected to MySQL"
                }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @property
    def description(self):
        description = f"MySQL client for database '{
            self.database}' at {self.host}:{self.port}"
        return description
