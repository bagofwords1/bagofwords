from app.data_sources.clients.base import DataSourceClient

import pandas as pd
import sqlalchemy
from sqlalchemy import text
from contextlib import contextmanager
from typing import List, Generator
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property


class PostgresqlClient(DataSourceClient):
    def __init__(self, host, port, database, user, password="", schema=None):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        # Optional schema or comma-separated list of schemas
        self.schema = schema
        self._schemas = []
        if isinstance(self.schema, str) and self.schema.strip():
            parts = [s.strip() for s in self.schema.split(",") if s.strip()]
            # Dedupe while preserving order
            seen = set()
            for p in parts:
                low = p  # keep case as provided; Postgres lowercases unquoted names
                if low not in seen:
                    seen.add(low)
                    self._schemas.append(low)

    @cached_property
    def pg_uri(self):
        uri = (
            f"postgresql://{self.user}:{self.password}@"
            f"{self.host}:{self.port}/{self.database}"
        )

        return uri

    @contextmanager
    def connect(self) -> Generator[sqlalchemy.engine.base.Connection, None, None]:
        """Yield a connection to a Postgres db."""
        engine = None
        conn = None
        try:
            engine = sqlalchemy.create_engine(self.pg_uri)
            conn = engine.connect()
            # Set search_path if schemas are provided
            if self._schemas:
                search_path = ", ".join(self._schemas)
                try:
                    conn.execute(text(f"SET search_path TO {search_path}"))
                except Exception:
                    pass
            yield conn
        except Exception as e:
            raise RuntimeError(f"{e}")
        finally:
            if conn is not None:
                conn.close()
            if engine is not None:
                engine.dispose()

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
        """Get tables with column/table comments via pg_description. May fail on some configurations."""
        with self.connect() as conn:
            params = {"database": self.database}
            where_clauses = [
                "c.table_catalog = :database",
                "c.table_schema NOT IN ('information_schema', 'pg_catalog')",
            ]
            if self._schemas:
                in_keys = []
                for idx, sch in enumerate(self._schemas):
                    key = f"s{idx}"
                    params[key] = sch
                    in_keys.append(f":{key}")
                where_clauses.append(f"c.table_schema IN ({', '.join(in_keys)})")

            where_sql = " WHERE " + " AND ".join(where_clauses)
            sql = text(f"""
                SELECT
                    c.table_schema,
                    c.table_name,
                    c.column_name,
                    c.data_type,
                    col_desc.description AS column_comment,
                    tbl_desc.description AS table_comment
                FROM information_schema.columns c
                LEFT JOIN pg_catalog.pg_statio_all_tables st
                    ON c.table_schema = st.schemaname AND c.table_name = st.relname
                LEFT JOIN pg_catalog.pg_description col_desc
                    ON col_desc.objoid = st.relid AND col_desc.objsubid = c.ordinal_position
                LEFT JOIN pg_catalog.pg_description tbl_desc
                    ON tbl_desc.objoid = st.relid AND tbl_desc.objsubid = 0
                {where_sql}
                ORDER BY c.table_schema, c.table_name, c.ordinal_position
            """)
            result = conn.execute(sql, params).fetchall()

            tables = {}
            for row in result:
                table_schema, table_name, column_name, data_type, col_comment, tbl_comment = row
                key = (table_schema, table_name)
                fqn = f"{table_schema}.{table_name}"
                if key not in tables:
                    tables[key] = Table(
                        name=fqn,
                        description=tbl_comment,
                        columns=[],
                        pks=[],
                        fks=[],
                        metadata_json={"schema": table_schema}
                    )
                tables[key].columns.append(TableColumn(
                    name=column_name,
                    dtype=data_type,
                    description=col_comment
                ))

            # Materialized views are not exposed via information_schema, so fetch
            # them separately from pg_catalog and merge into the result.
            self._append_materialized_views(conn, tables, with_comments=True)
            return list(tables.values())

    def _append_materialized_views(self, conn, tables: dict, with_comments: bool) -> None:
        """Append materialized view columns (and optional comments) to `tables`.

        Materialized views (pg_class.relkind = 'm') are a Postgres-specific
        object and are not present in information_schema, so the standard table
        queries miss them. This reads them directly from pg_catalog.
        """
        params = {}
        where_clauses = [
            "c.relkind = 'm'",
            "n.nspname NOT IN ('information_schema', 'pg_catalog')",
        ]
        if self._schemas:
            in_keys = []
            for idx, sch in enumerate(self._schemas):
                key = f"mv_s{idx}"
                params[key] = sch
                in_keys.append(f":{key}")
            where_clauses.append(f"n.nspname IN ({', '.join(in_keys)})")
        where_sql = " WHERE " + " AND ".join(where_clauses)

        if with_comments:
            select_extra = (
                ",\n                col_desc.description AS column_comment,"
                "\n                tbl_desc.description AS table_comment"
            )
            join_extra = """
            LEFT JOIN pg_catalog.pg_description col_desc
                ON col_desc.objoid = c.oid AND col_desc.objsubid = a.attnum
            LEFT JOIN pg_catalog.pg_description tbl_desc
                ON tbl_desc.objoid = c.oid AND tbl_desc.objsubid = 0"""
        else:
            select_extra = ""
            join_extra = ""

        sql = text(f"""
            SELECT
                n.nspname AS table_schema,
                c.relname AS table_name,
                a.attname AS column_name,
                format_type(a.atttypid, a.atttypmod) AS data_type{select_extra}
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n
                ON n.oid = c.relnamespace
            JOIN pg_catalog.pg_attribute a
                ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped{join_extra}
            {where_sql}
            ORDER BY n.nspname, c.relname, a.attnum
        """)
        result = conn.execute(sql, params).fetchall()

        for row in result:
            if with_comments:
                table_schema, table_name, column_name, data_type, col_comment, tbl_comment = row
            else:
                table_schema, table_name, column_name, data_type = row
                col_comment = tbl_comment = None
            key = (table_schema, table_name)
            fqn = f"{table_schema}.{table_name}"
            if key not in tables:
                tables[key] = Table(
                    name=fqn,
                    description=tbl_comment,
                    columns=[],
                    pks=[],
                    fks=[],
                    metadata_json={"schema": table_schema, "is_materialized_view": True}
                )
            tables[key].columns.append(TableColumn(
                name=column_name,
                dtype=data_type,
                description=col_comment
            ))

    def _get_tables_basic(self) -> List[Table]:
        """Get tables without comments (original query - always works)."""
        try:
            with self.connect() as conn:
                params = {"database": self.database}
                where_clauses = [
                    "table_catalog = :database",
                    "table_schema NOT IN ('information_schema', 'pg_catalog')",
                ]
                if self._schemas:
                    in_keys = []
                    for idx, sch in enumerate(self._schemas):
                        key = f"s{idx}"
                        params[key] = sch
                        in_keys.append(f":{key}")
                    where_clauses.append(f"table_schema IN ({', '.join(in_keys)})")

                where_sql = " WHERE " + " AND ".join(where_clauses)
                sql = text(f"""
                    SELECT table_schema, table_name, column_name, data_type
                    FROM information_schema.columns
                    {where_sql}
                    ORDER BY table_schema, table_name, ordinal_position
                """)
                result = conn.execute(sql, params).fetchall()

                tables = {}
                for row in result:
                    table_schema, table_name, column_name, data_type = row
                    key = (table_schema, table_name)
                    fqn = f"{table_schema}.{table_name}"
                    if key not in tables:
                        tables[key] = Table(
                            name=fqn, columns=[], pks=[], fks=[], metadata_json={"schema": table_schema}
                        )
                    tables[key].columns.append(TableColumn(name=column_name, dtype=data_type))

                # Materialized views are absent from information_schema; merge them in.
                self._append_materialized_views(conn, tables, with_comments=False)
                return list(tables.values())
        except Exception as e:
            print(f"Error retrieving tables: {e}")
            return []

    def get_schema(self, table_id: str) -> Table:
        """This method is now obsolete. Please use get_tables() instead."""
        raise NotImplementedError(
            "get_schema() is obsolete. Use get_tables() instead.")

    def get_schemas(self):
        """Get schemas for all tables in the specified database."""
        return self.get_tables()

    def prompt_schema(self):
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        """Test connection to PostgreSQL and return status information."""
        try:
            with self.connect() as conn:
                conn.execute(text("SELECT 1"))
                return {
                    "success": True,
                    "message": "Successfully connected to PostgreSQL"
                }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
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
        description = f"Postgresql database at {self.host}:{self.port}/{self.database}\n\n"
        description += system_prompt

        return description
