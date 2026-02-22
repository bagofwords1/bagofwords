cat > /app/backend/app/data_sources/clients/sybase_client.py << 'EOF'
from app.data_sources.clients.base import DataSourceClient

import pyodbc
import pandas as pd
from contextlib import contextmanager
from typing import Generator, List
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter


class SybaseClient(DataSourceClient):
    def __init__(self, host, port, database, user, password):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password

    def _connection_string(self):
        return (
            f"DRIVER={{FreeTDS}};"
            f"SERVER={self.host};"
            f"PORT={self.port};"
            f"DATABASE={self.database};"
            f"UID={self.user};"
            f"PWD={self.password};"
            f"TDS_Version=5.0;"
        )

    @contextmanager
    def connect(self) -> Generator[pyodbc.Connection, None, None]:
        """Yield a raw pyodbc connection to a Sybase SQL Anywhere database."""
        conn = None
        try:
            conn = pyodbc.connect(self._connection_string())
            yield conn
        except Exception as e:
            raise RuntimeError(f"{e}")
        finally:
            if conn is not None:
                conn.close()

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute SQL statement and return the result as a DataFrame."""
        try:
            with self.connect() as conn:
                df = pd.read_sql(sql, conn)
            return df
        except Exception as e:
            print(f"Error executing SQL: {e}")
            raise

    def get_tables(self) -> List[Table]:
        """Get tables from the database using SYS views."""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT t.table_name, c.column_name, d.domain_name AS data_type
                    FROM SYS.SYSTABCOL c
                    JOIN SYS.SYSTAB t ON c.table_id = t.table_id
                    JOIN SYS.SYSDOMAIN d ON c.domain_id = d.domain_id
                    WHERE t.creator NOT IN (0, 3)
                    ORDER BY t.table_name, c.column_id
                """)
                rows = cursor.fetchall()

                tables = {}
                for row in rows:
                    table_name, column_name, data_type = row

                    if table_name not in tables:
                        tables[table_name] = Table(
                            name=table_name, columns=[], pks=None, fks=None)
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
        """Test connection to Sybase SQL Anywhere and return status information."""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                return {
                    "success": True,
                    "message": "Successfully connected to Sybase SQL Anywhere"
                }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @property
    def description(self):
        system_prompt = """
        This is a Sybase SQL Anywhere database (Watcom SQL dialect).
        You can call the execute_query method to run SQL queries.

        ```python
        df = client.execute_query("SELECT TOP 10 * FROM employees ORDER BY name")
        ```
        or:
        ```python
        df = client.execute_query("SELECT department, COUNT(*) AS cnt FROM employees GROUP BY department")
        ```

        IMPORTANT - Sybase SQL Anywhere dialect differences:

        Pagination: use TOP n or LIMIT n. "TOP 5 START AT 11" skips 10 rows (1-based). FETCH FIRST is NOT supported.
        Current date/time: NOW(), GETDATE(), TODAY(), CURRENT DATE / CURRENT TIMESTAMP (space, not underscore).
        Date arithmetic: DATEADD(day, 7, date), DATEDIFF(day, d1, d2), DATEPART(year, date) or YEAR(date).
        Date formatting: DATEFORMAT(date, 'YYYY-MM-DD HH:NN:SS') — minutes are NN, not MI.
        String aggregation: LIST(col, ', ') — not STRING_AGG or GROUP_CONCAT.
        Concatenation: || treats NULL as '' (does not propagate NULL). STRING(a, b, c) also works.
        NULL handling: ISNULL(a, b) or COALESCE(a, b) both work.
        Find in string: LOCATE(haystack, needle) or CHARINDEX(needle, haystack).
        Boolean: BIT type with 0/1, not TRUE/FALSE.

        DO NOT use: EXTRACT(), INTERVAL, TO_CHAR(), STRING_AGG(), ILIKE, GENERATE_SERIES(), FETCH FIRST.
        """
        description = f"Sybase SQL Anywhere database at {self.host}:{self.port}/{self.database}\n\n"
        description += system_prompt

        return description
EOF
