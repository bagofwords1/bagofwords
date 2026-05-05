from app.data_sources.clients.base import DataSourceClient

import pyodbc
import pandas as pd
from contextlib import contextmanager
from typing import Generator, List, Optional
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter


QUERY_TIMEOUT_SECONDS = 60


class OdbcClient(DataSourceClient):
    """Generic ODBC client. Works with any installed ODBC driver
    (Progress OpenEdge, Informix, Teradata, SQLite, Postgres, ...) by
    delegating SQL flavor concerns to the driver and using ODBC's
    standard catalog APIs (SQLTables / SQLColumns) for schema discovery.

    Connection string assembly accepts either a registered DSN or an
    explicit driver name plus optional host/port/database, and a
    free-form `extra_params` string for driver-specific options.
    """

    def __init__(
        self,
        dsn: Optional[str] = None,
        driver: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        database: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        schema: Optional[str] = None,
        extra_params: Optional[str] = None,
    ):
        if not dsn and not driver:
            raise ValueError("ODBC connection requires either a DSN or a driver name")
        self.dsn = dsn.strip() if isinstance(dsn, str) and dsn.strip() else None
        self.driver = driver.strip() if isinstance(driver, str) and driver.strip() else None
        self.host = host
        self.port = int(port) if port not in (None, "") else None
        self.database = database
        self.user = user
        self.password = password
        self.schema = schema
        self.extra_params = extra_params
        self._schemas: List[str] = []
        if isinstance(self.schema, str) and self.schema.strip():
            seen = set()
            for p in (s.strip() for s in self.schema.split(",")):
                if p and p not in seen:
                    seen.add(p)
                    self._schemas.append(p)

    def _connection_string(self) -> str:
        parts: List[str] = []
        if self.dsn:
            parts.append(f"DSN={self.dsn}")
        if self.driver:
            parts.append(f"DRIVER={{{self.driver}}}")
        if self.host:
            parts.append(f"SERVER={self.host}")
            parts.append(f"HOST={self.host}")
        if self.port is not None:
            parts.append(f"PORT={self.port}")
        if self.database:
            parts.append(f"DATABASE={self.database}")
            parts.append(f"DB={self.database}")
        if self.user:
            parts.append(f"UID={self.user}")
        if self.password:
            parts.append(f"PWD={self.password}")
        conn_str = ";".join(parts)
        if self.extra_params and self.extra_params.strip():
            extra = self.extra_params.strip().rstrip(";")
            conn_str = f"{conn_str};{extra}"
        return conn_str

    @contextmanager
    def connect(self) -> Generator[pyodbc.Connection, None, None]:
        """Yield a raw pyodbc connection."""
        conn = None
        try:
            conn = pyodbc.connect(self._connection_string(), timeout=QUERY_TIMEOUT_SECONDS)
            yield conn
        except Exception as e:
            raise RuntimeError(str(e))
        finally:
            if conn is not None:
                conn.close()

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute SQL statement and return the result as a DataFrame.

        Drives pyodbc directly rather than `pd.read_sql` to avoid
        pandas' SQLAlchemy-only warning for raw DBAPI connections.
        """
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute(sql)
                if cursor.description is None:
                    return pd.DataFrame()
                columns = [d[0] for d in cursor.description]
                rows = cursor.fetchall()
                return pd.DataFrame.from_records([tuple(r) for r in rows], columns=columns)
        except Exception as e:
            print(f"Error executing SQL: {e}")
            raise

    def get_tables(self) -> List[Table]:
        """Discover tables and columns via ODBC catalog APIs (SQLTables / SQLColumns).

        Driver-agnostic: works for any compliant ODBC driver without
        assuming a specific INFORMATION_SCHEMA dialect.
        """
        try:
            with self.connect() as conn:
                cursor = conn.cursor()

                schema_filter = set(self._schemas) if self._schemas else None
                table_keys: List[tuple] = []
                seen = set()
                for row in cursor.tables(tableType="TABLE"):
                    table_schem = row.table_schem
                    table_name = row.table_name
                    if not table_name:
                        continue
                    if schema_filter and table_schem not in schema_filter:
                        continue
                    key = (table_schem, table_name)
                    if key in seen:
                        continue
                    seen.add(key)
                    table_keys.append(key)

                tables: List[Table] = []
                for table_schem, table_name in table_keys:
                    columns: List[TableColumn] = []
                    try:
                        col_cursor = conn.cursor()
                        col_rows = col_cursor.columns(table=table_name, schema=table_schem)
                        for c in col_rows:
                            columns.append(TableColumn(
                                name=c.column_name,
                                dtype=c.type_name,
                                description=getattr(c, "remarks", None) or None,
                            ))
                    except Exception as e:
                        print(f"Could not list columns for {table_schem}.{table_name}: {e}")

                    fqn = f"{table_schem}.{table_name}" if table_schem else table_name
                    tables.append(Table(
                        name=fqn,
                        columns=columns,
                        pks=None,
                        fks=None,
                        metadata_json={"schema": table_schem} if table_schem else None,
                    ))
                return tables
        except Exception as e:
            print(f"Error retrieving tables: {e}")
            return []

    def get_schema(self, table_id: str) -> Table:
        """This method is now obsolete. Please use get_tables() instead."""
        raise NotImplementedError(
            "get_schema() is obsolete. Use get_tables() instead.")

    def get_schemas(self):
        return self.get_tables()

    def prompt_schema(self):
        return TableFormatter(self.get_schemas()).table_str

    def test_connection(self):
        """Probe connection. Try `SELECT 1` first; fall back to a no-op
        catalog call so drivers that don't support SELECT-without-FROM
        (rare, but seen in some pure-DSN setups) still pass.
        """
        try:
            with self.connect() as conn:
                cur = conn.cursor()
                try:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                except Exception:
                    cur.tables().fetchone()
                return {
                    "success": True,
                    "message": "Successfully connected via ODBC",
                }
        except Exception as e:
            return {
                "success": False,
                "message": str(e),
            }

    @property
    def description(self):
        target = self.dsn and f"DSN={self.dsn}" or f"DRIVER={self.driver}"
        if self.host:
            target += f" @ {self.host}"
            if self.port is not None:
                target += f":{self.port}"
        if self.database:
            target += f"/{self.database}"
        return f"Generic ODBC connection ({target})"
