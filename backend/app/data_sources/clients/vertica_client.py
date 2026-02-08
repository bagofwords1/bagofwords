from app.data_sources.clients.base import DataSourceClient

import pandas as pd
from typing import List
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property

try:
    import verticapy as vp
except ImportError:
    vp = None


class VerticaClient(DataSourceClient):
    def __init__(self, host, port, database, user, password, schema="public", **kwargs):
        if vp is None:
            raise ImportError("verticapy is required for Vertica connections. Please install it with: pip install verticapy")
        
        self.host = host
        self.port = port
        self.database = database
        self.schema = schema
        self.user = user
        self.password = password
        self._connection_name = f"vertica_conn_{hash(f'{host}_{port}_{database}_{user}_{schema}')}"
        self._connected = False

    @cached_property
    def connection_params(self):
        return {
            "host": self.host,
            "port": str(self.port),
            "database": self.database,
            "user": self.user,
            "password": self.password
        }

    def connect(self):
        """Establish connection to Vertica using verticapy."""
        try:
            if not self._connected:
                # Create new connection in verticapy
                vp.new_connection(
                    self.connection_params,
                    name=self._connection_name
                )
                # Set this connection as active
                vp.connect(self._connection_name)
                self._connected = True
            return self._connection_name
        except Exception as e:
            raise RuntimeError(f"Failed to connect to Vertica: {e}")

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute SQL statement and return the result as a DataFrame."""
        try:
            # Connect to Vertica
            self.connect()
            
            # Execute query using verticapy vDataFrame
            result = vp.vDataFrame(sql)
            
            # Convert to pandas DataFrame
            df = result.to_pandas()
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
        """Get tables with column comments. May fail on some Vertica configurations."""
        self.connect()

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

        result_df = vp.vDataFrame(query)
        result = result_df.to_pandas()

        tables = {}
        for _, row in result.iterrows():
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
        try:
            self.connect()

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

            result_df = vp.vDataFrame(query)
            result = result_df.to_pandas()

            tables = {}
            for _, row in result.iterrows():
                table_name = row['table_name']
                column_name = row['column_name']
                data_type = row['data_type']

                if table_name not in tables:
                    tables[table_name] = Table(
                        name=table_name, columns=[], pks=[], fks=[], metadata_json={"schema": self.schema})
                tables[table_name].columns.append(
                    TableColumn(name=column_name, dtype=data_type))

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
        """Test connection to Vertica and return status information."""
        try:
            self.connect()
            
            # Test with a simple query
            test_df = vp.vDataFrame("SELECT 1 as test")
            test_result = test_df.to_pandas()
            
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
        You can call the execute_query method to run SQL queries using verticapy.
        
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

    def __del__(self):
        """Clean up connection when object is destroyed."""
        if self._connected:
            try:
                # verticapy manages connections internally
                # No explicit cleanup needed
                pass
            except:
                pass