from app.data_sources.clients.base import DataSourceClient

import logging
import pandas as pd
from contextlib import contextmanager
from typing import Generator, List, Optional

from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter

try:
    import teradatasql
except ImportError:
    teradatasql = None

logger = logging.getLogger(__name__)

QUERY_TIMEOUT_SECONDS = 60

# Teradata stores column types as a 2-char code in DBC.ColumnsV. Map the common
# ones to readable type names for the agent prompt; fall back to the raw code.
_TD_TYPE_CODES = {
    "I1": "BYTEINT",
    "I2": "SMALLINT",
    "I8": "BIGINT",
    "I": "INTEGER",
    "D": "DECIMAL",
    "F": "FLOAT",
    "N": "NUMBER",
    "CF": "CHAR",
    "CV": "VARCHAR",
    "CO": "CLOB",
    "DA": "DATE",
    "AT": "TIME",
    "TS": "TIMESTAMP",
    "TZ": "TIME WITH TIME ZONE",
    "SZ": "TIMESTAMP WITH TIME ZONE",
    "BF": "BYTE",
    "BV": "VARBYTE",
    "BO": "BLOB",
    "JN": "JSON",
    "XM": "XML",
    "DY": "INTERVAL DAY",
    "DH": "INTERVAL DAY TO HOUR",
    "DM": "INTERVAL DAY TO MINUTE",
    "DS": "INTERVAL DAY TO SECOND",
    "YR": "INTERVAL YEAR",
    "YM": "INTERVAL YEAR TO MONTH",
    "MO": "INTERVAL MONTH",
    "HR": "INTERVAL HOUR",
    "HM": "INTERVAL HOUR TO MINUTE",
    "HS": "INTERVAL HOUR TO SECOND",
    "MI": "INTERVAL MINUTE",
    "MS": "INTERVAL MINUTE TO SECOND",
    "SC": "INTERVAL SECOND",
}


class TeradataClient(DataSourceClient):
    def __init__(self, host, database, user, password, port: int = 1025,
                 logmech: Optional[str] = "TD2"):
        if teradatasql is None:
            raise ImportError(
                "teradatasql is required for Teradata connections. "
                "Please install it with: pip install teradatasql"
            )
        self.host = host
        self.port = int(port)
        self.database = database
        self.user = user
        self.password = password
        # Empty/None logmech means use the driver default (TD2).
        self.logmech = (logmech or "").strip() or None

        # `database` may be a single database or a comma-separated list. In
        # Teradata a "database" is the namespace (≈ schema in other engines).
        self._databases: List[str] = []
        if isinstance(self.database, str) and self.database.strip():
            seen = set()
            for part in self.database.split(","):
                part = part.strip()
                if part and part not in seen:
                    seen.add(part)
                    self._databases.append(part)

    @contextmanager
    def connect(self) -> Generator["teradatasql.TeradataConnection", None, None]:
        """Yield a raw teradatasql DBAPI connection to a Teradata database."""
        conn_kwargs = {
            "host": self.host,
            "user": self.user,
            "password": self.password,
            "dbs_port": str(self.port),
        }
        if self.logmech:
            conn_kwargs["logmech"] = self.logmech
        # Default the session database to the first configured database.
        if self._databases:
            conn_kwargs["database"] = self._databases[0]

        conn = None
        try:
            conn = teradatasql.connect(**conn_kwargs)
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

    def _format_dtype(self, code: Optional[str]) -> str:
        if not code:
            return ""
        key = code.strip()
        return _TD_TYPE_CODES.get(key, key)

    def get_tables(self) -> List[Table]:
        """Get tables and columns from the DBC system views."""
        with self.connect() as conn:
            cursor = conn.cursor()

            where_clauses = ["t.TableKind IN ('T','V','O','Q')"]
            params: list = []
            if self._databases:
                placeholders = ", ".join(["?"] * len(self._databases))
                where_clauses.append(f"c.DatabaseName IN ({placeholders})")
                params.extend(self._databases)

            where_sql = " AND ".join(where_clauses)
            sql = f"""
                SELECT
                    c.DatabaseName,
                    c.TableName,
                    c.ColumnName,
                    c.ColumnType,
                    c.CommentString AS column_comment,
                    t.CommentString AS table_comment
                FROM DBC.ColumnsV c
                JOIN DBC.TablesV t
                    ON c.DatabaseName = t.DatabaseName
                    AND c.TableName = t.TableName
                WHERE {where_sql}
                ORDER BY c.DatabaseName, c.TableName, c.ColumnId
            """
            cursor.execute(sql, params)
            rows = cursor.fetchall()

            tables = {}
            for row in rows:
                database_name, table_name, column_name, column_type, col_comment, tbl_comment = row
                # System views often pad CHAR columns with trailing spaces.
                database_name = (database_name or "").strip()
                table_name = (table_name or "").strip()
                column_name = (column_name or "").strip()
                col_comment = col_comment.strip() if isinstance(col_comment, str) else col_comment
                tbl_comment = tbl_comment.strip() if isinstance(tbl_comment, str) else tbl_comment

                key = (database_name, table_name)
                fqn = f"{database_name}.{table_name}"

                if key not in tables:
                    tables[key] = Table(
                        name=fqn,
                        description=tbl_comment or None,
                        columns=[],
                        pks=None,
                        fks=None,
                        metadata_json={"schema": database_name},
                    )
                tables[key].columns.append(TableColumn(
                    name=column_name,
                    dtype=self._format_dtype(column_type),
                    description=col_comment or None,
                ))
            return list(tables.values())

    def get_schema(self, table_id: str) -> Table:
        """This method is now obsolete. Please use get_tables() instead."""
        raise NotImplementedError(
            "get_schema() is obsolete. Use get_tables() instead.")

    def get_schemas(self):
        """Get schemas for all tables in the specified database(s)."""
        return self.get_tables()

    def prompt_schema(self):
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        """Test connection to Teradata and return status information."""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                return {
                    "success": True,
                    "message": "Successfully connected to Teradata"
                }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @property
    def description(self):
        system_prompt = """
        This is a Teradata Vantage database (Teradata SQL dialect).
        You can call the execute_query method to run SQL queries.

        Table references: tables are provided as database.table (Teradata
        "databases" are the namespace, equivalent to schemas elsewhere). Use
        them exactly as given, without quotes — e.g. SELECT * FROM sales.orders.

        ```python
        df = client.execute_query("SELECT TOP 10 * FROM sales.orders ORDER BY order_date DESC")
        ```

        IMPORTANT - Teradata dialect differences:

        Pagination: use TOP n (between SELECT and the column list), e.g.
            SELECT TOP 100 * FROM sales.orders. LIMIT is NOT supported.
            You may also use QUALIFY ROW_NUMBER() OVER (...) <= n.
        Current date/time: CURRENT_DATE, CURRENT_TIMESTAMP, CURRENT_TIME.
        Date arithmetic: date + INTERVAL '7' DAY, ADD_MONTHS(date, n),
            EXTRACT(YEAR FROM date). Use CAST(x AS DATE) for casts.
        Window filtering: QUALIFY filters window functions directly, e.g.
            SELECT ... QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1.
        Sampling: SAMPLE n (rows) or SAMPLE 0.1 (fraction).
        String aggregation: use XMLAGG(...) or stringagg patterns; there is no
            GROUP_CONCAT.
        Concatenation: || (NULL propagates — wrap with COALESCE if needed).
        NULL handling: COALESCE(a, b) and NULLIF(a, b) are supported.
        Not-equal: use <> (preferred) or !=.

        DO NOT use: LIMIT, ILIKE, GENERATE_SERIES(), STRING_AGG().

        Performance notes:
        - Avoid wrapping filtered/joined columns in functions — breaks index use.
        - Avoid leading-wildcard LIKE '%x%' on large tables.
        - Prefer exact match or IN (...) over text search on large columns.
        - Queries have a 60s timeout. Narrow date ranges first; don't rely on retries.
        """
        description = f"Teradata database '{self.database}' at {self.host}:{self.port}\n\n"
        description += system_prompt
        return description
