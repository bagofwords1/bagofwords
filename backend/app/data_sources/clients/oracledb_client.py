from app.data_sources.clients.base import DataSourceClient

import pandas as pd
import sqlalchemy
from sqlalchemy import text
from contextlib import contextmanager
from typing import Generator, List, Optional
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property


class OracledbClient(DataSourceClient):
    def __init__(self, host, port, service_name, user, password, schema: Optional[str] = None):
        self.host = host
        self.port = port
        self.service_name = service_name
        self.user = user
        self.password = password
        # Optional schema or comma-separated list of schemas
        self.schema = schema
        self._schemas = []
        if isinstance(self.schema, str) and self.schema.strip():
            parts = [s.strip() for s in self.schema.split(",") if s.strip()]
            seen = set()
            for p in parts:
                up = p.upper()
                if up not in seen:
                    seen.add(up)
                    self._schemas.append(up)

    @cached_property
    def oracle_uri(self):
        uri = (
            f"oracle+oracledb://{self.user}:{self.password}@"
            f"{self.host}:{self.port}/?service_name={self.service_name}"
        )
        return uri

    @contextmanager
    def connect(self) -> Generator[sqlalchemy.engine.base.Connection, None, None]:
        """Yield a connection to an Oracle database."""
        engine = None
        conn = None
        try:
            engine = sqlalchemy.create_engine(self.oracle_uri)
            conn = engine.connect()
            # Set current schema if provided (Oracle has no search_path; use first schema)
            if self._schemas:
                current_schema = self._schemas[0]
                try:
                    conn.execute(text(f'ALTER SESSION SET CURRENT_SCHEMA = {current_schema}'))
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
        """Get tables with column/table comments. May fail on some Oracle configurations."""
        with self.connect() as conn:
            params = {}
            where_clauses = []
            if self._schemas:
                in_keys = []
                for idx, sch in enumerate(self._schemas):
                    key = f"o{idx}"
                    params[key] = sch
                    in_keys.append(f":{key}")
                where_clauses.append(f"c.owner IN ({', '.join(in_keys)})")
            else:
                params["owner"] = self.user.upper()
                where_clauses.append("c.owner = :owner")

            where_sql = " WHERE " + " AND ".join(where_clauses)
            sql = text(f"""
                SELECT
                    c.owner,
                    c.table_name,
                    c.column_name,
                    c.data_type,
                    cc.comments AS column_comment,
                    tc.comments AS table_comment
                FROM all_tab_columns c
                LEFT JOIN all_col_comments cc
                    ON c.owner = cc.owner
                    AND c.table_name = cc.table_name
                    AND c.column_name = cc.column_name
                LEFT JOIN all_tab_comments tc
                    ON c.owner = tc.owner
                    AND c.table_name = tc.table_name
                {where_sql}
                ORDER BY c.owner, c.table_name, c.column_id
            """)
            result = conn.execute(sql, params).fetchall()

            tables = {}
            for row in result:
                owner, table_name, column_name, data_type, col_comment, tbl_comment = row
                key = (owner, table_name)
                fqn = f"{owner}.{table_name}"
                if key not in tables:
                    tables[key] = Table(
                        name=fqn,
                        description=tbl_comment if tbl_comment else None,
                        columns=[],
                        pks=[],
                        fks=[],
                        metadata_json={"schema": owner}
                    )
                tables[key].columns.append(TableColumn(
                    name=column_name,
                    dtype=data_type,
                    description=col_comment if col_comment else None
                ))
            return list(tables.values())

    def _get_tables_basic(self) -> List[Table]:
        """Get tables without comments (original query - always works)."""
        try:
            with self.connect() as conn:
                params = {}
                where_clauses = []
                if self._schemas:
                    in_keys = []
                    for idx, sch in enumerate(self._schemas):
                        key = f"o{idx}"
                        params[key] = sch
                        in_keys.append(f":{key}")
                    where_clauses.append(f"owner IN ({', '.join(in_keys)})")
                else:
                    params["owner"] = self.user.upper()
                    where_clauses.append("owner = :owner")

                where_sql = " WHERE " + " AND ".join(where_clauses)
                sql = text(f"""
                    SELECT owner, table_name, column_name, data_type
                    FROM all_tab_columns
                    {where_sql}
                    ORDER BY owner, table_name, column_id
                """)
                result = conn.execute(sql, params).fetchall()

                tables = {}
                for row in result:
                    owner, table_name, column_name, data_type = row
                    key = (owner, table_name)
                    fqn = f"{owner}.{table_name}"
                    if key not in tables:
                        tables[key] = Table(
                            name=fqn, columns=[], pks=[], fks=[], metadata_json={"schema": owner}
                        )
                    tables[key].columns.append(TableColumn(name=column_name, dtype=data_type))
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
        """Test connection to Oracle and return status information."""
        try:
            with self.connect() as conn:
                conn.execute(text("SELECT 1 FROM DUAL"))
                return {
                    "success": True,
                    "message": "Successfully connected to Oracle"
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
        df = client.execute_query("SELECT * FROM USERS")
        ```
        or:
        ```python
        df = client.execute_query("SELECT * FROM HR.EMPLOYEES WHERE SALARY > 100000")
        ```
        """
        description = f"Oracle database service '{self.service_name}' at {self.host}:{self.port}\n\n"
        description += system_prompt
        return description
