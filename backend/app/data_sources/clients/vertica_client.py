from app.data_sources.clients.progress import discovery_progress, discovery_items, discovery_phase, IndexingCancelled
from app.data_sources.clients.base import DataSourceClient

import logging

import pandas as pd
from contextlib import contextmanager
from typing import List
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property

try:
    import vertica_python
except ImportError:
    vertica_python = None

logger = logging.getLogger(__name__)


class VerticaClient(DataSourceClient):
    def __init__(self, host, port, database, user, password, schema="public", **kwargs):
        if vertica_python is None:
            raise ImportError(
                "vertica-python is required for Vertica connections. Please install it with: pip install vertica-python")

        self.host = host
        self.port = port
        self.database = database
        self.schema = schema
        self.user = user
        self.password = password

    @cached_property
    def connection_params(self):
        """The driver's connection details, as verticapy passed them before
        (the port as a number, undecodable bytes ignored)."""
        return {
            "host": self.host,
            "port": int(self.port),
            "database": self.database,
            "user": self.user,
            "password": self.password,
            "unicode_error": "ignore",
        }

    @contextmanager
    def connect(self):
        """A vertica_python connection of this client's own, for one call,
        closed after it. Nothing is shared between calls or clients (verticapy
        kept one connection for the whole process, which the last client to
        connect replaced) and no credentials are written to disk."""
        try:
            conn = vertica_python.connect(**self.connection_params)
        except IndexingCancelled:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to connect to Vertica: {e}") from e
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception as e:
                # The type only: a driver message may hold connection details.
                logger.warning("vertica: closing the connection failed (%s)", type(e).__name__)

    def _frame(self, sql: str) -> pd.DataFrame:
        """Run `sql` once on a connection of this client's own and return
        the result as vDataFrame.to_pandas built it: the driver's rows, the
        cursor's column names, every row, and an empty result as an empty
        frame."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall()
            columns = [column[0] for column in (cursor.description or [])]
        df = pd.DataFrame(rows)
        if not df.empty:
            df.columns = columns
        return df

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute SQL statement and return the result as a DataFrame.

        `sql` is sent exactly as given (the read-only guard checked this
        text), once, on a vertica_python cursor. Not through verticapy's
        vDataFrame, which rewrites a query before sending it."""
        try:
            return self._frame(sql)
        except Exception as e:
            print(f"Error executing SQL: {e}")
            raise

    def get_tables(self) -> List[Table]:
        """Get tables with graceful fallback if enriched query fails."""
        try:
            return self._get_tables_enriched()
        except IndexingCancelled:
            raise
        except Exception:
            return self._get_tables_basic()

    def _get_tables_enriched(self) -> List[Table]:
        """Get tables with column comments. May fail on some Vertica configurations."""
        discovery_phase('reading_columns')

        # Query with column comments (Vertica supports comments in v_catalog.comments)
        query = f"""
        SELECT
            t.table_schema,
            t.table_name,
            'TABLE' AS object_type,
            c.column_name,
            c.data_type,
            cm.comment AS column_comment,
            tcm.comment AS table_comment
        FROM v_catalog.tables t
        JOIN v_catalog.columns c
            ON t.table_id = c.table_id AND t.table_schema = c.table_schema
        LEFT JOIN v_catalog.comments cm
            ON cm.object_id = c.column_id
            AND cm.object_type = 'COLUMN'
        LEFT JOIN v_catalog.comments tcm
            ON tcm.object_id = t.table_id
            AND tcm.object_type = 'TABLE'
        WHERE t.table_schema = '{self.schema}'

        UNION ALL

        SELECT
            v.table_schema,
            v.table_name,
            'VIEW' AS object_type,
            c.column_name,
            c.data_type,
            cm.comment AS column_comment,
            vcm.comment AS table_comment
        FROM v_catalog.views v
        JOIN v_catalog.columns c
            ON v.table_id = c.table_id AND v.table_schema = c.table_schema
        LEFT JOIN v_catalog.comments cm
            ON cm.object_id = c.column_id
            AND cm.object_type = 'COLUMN'
        LEFT JOIN v_catalog.comments vcm
            ON vcm.object_id = v.table_id
            AND vcm.object_type = 'VIEW'
        WHERE v.table_schema = '{self.schema}'

        ORDER BY table_name, column_name;
        """

        result = self._frame(query)

        tables = {}
        for _, row in discovery_items(result.iterrows(), 'columns', total=len(result), label=lambda pair: str(pair[1]['table_name']) + '.' + str(pair[1]['column_name'])):
            table_name = row['table_name']
            column_name = row['column_name']
            data_type = row['data_type']
            col_comment = row.get('column_comment')
            tbl_comment = row.get('table_comment')

            if table_name not in tables:
                tables[table_name] = Table(
                    name=table_name,
                    description=tbl_comment if tbl_comment else None,
                    columns=[],
                    pks=[],
                    fks=[],
                    metadata_json={"schema": self.schema}
                )
            tables[table_name].columns.append(TableColumn(
                name=column_name,
                dtype=data_type,
                description=col_comment if col_comment else None
            ))

        return list(tables.values())

    def _get_tables_basic(self) -> List[Table]:
        """Get tables without comments (original query - always works)."""
        discovery_phase('metadata_fallback')
        try:
            query = f"""
            SELECT
                t.table_schema,
                t.table_name,
                'TABLE' AS object_type,
                c.column_name,
                c.data_type
            FROM v_catalog.tables t
            JOIN v_catalog.columns c
            ON t.table_id = c.table_id AND t.table_schema = c.table_schema
            WHERE t.table_schema = '{self.schema}'

            UNION ALL

            SELECT
                v.table_schema,
                v.table_name,
                'VIEW' AS object_type,
                c.column_name,
                c.data_type
            FROM v_catalog.views v
            JOIN v_catalog.columns c
            ON v.table_id = c.table_id AND v.table_schema = c.table_schema
            WHERE v.table_schema = '{self.schema}'

            ORDER BY table_name, column_name;
            """

            result = self._frame(query)

            tables = {}
            for _, row in discovery_items(result.iterrows(), 'columns', total=len(result), label=lambda pair: str(pair[1]['table_name']) + '.' + str(pair[1]['column_name'])):
                table_name = row['table_name']
                column_name = row['column_name']
                data_type = row['data_type']

                if table_name not in tables:
                    tables[table_name] = Table(
                        name=table_name, columns=[], pks=[], fks=[], metadata_json={"schema": self.schema})
                tables[table_name].columns.append(
                    TableColumn(name=column_name, dtype=data_type))

            return list(tables.values())
        except IndexingCancelled:
            raise
        except Exception as e:
            self.mark_discovery_incomplete("vertica catalog")
            print(f"Error retrieving tables: {e}")
            return []

    def get_schema(self, table_id: str) -> Table:
        """This method is now obsolete. Please use get_tables() instead."""
        raise NotImplementedError(
            "get_schema() is obsolete. Use get_tables() instead.")

    @discovery_progress
    def get_schemas(self, progress_callback=None):
        """Get schemas for all tables in the specified database."""
        return self.get_tables()

    def prompt_schema(self):
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        """Test connection to Vertica and return status information."""
        try:
            # Test with a simple query
            test_result = self._frame("SELECT 1 as test")

            if not test_result.empty:
                return {
                    "success": True,
                    "message": "Successfully connected to Vertica"
                }
            else:
                return {
                    "success": False,
                    "message": "Connection test failed - no result returned"
                }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @property
    def description(self):
        system_prompt = """
        You can call the execute_query method to run SQL queries using vertica_python.
        
        The below are examples for how to use the execute_query method. Note that the actual SQL will vary based on the schema.
        Notice only the SQL syntax and instructions on how to use the execute_query method, not the actual SQL queries.

        ```python
        df = client.execute_query("SELECT * FROM users")
        ```
        or:
        ```python
        df = client.execute_query("SELECT * FROM users WHERE age > 30")
        ```

        Vertica is a columnar analytics database optimized for large-scale data warehousing and analytics workloads.
        """
        description = f"Vertica database at {self.host}:{self.port}/{self.database} (schema: {self.schema})\n\n"
        description += system_prompt

        return description