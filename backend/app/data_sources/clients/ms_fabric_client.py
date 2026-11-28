from app.data_sources.clients.progress import discovery_progress, discovery_items, discovery_phase, IndexingCancelled
from app.data_sources.clients.base import DataSourceClient

import logging
import hashlib
import json
import time
import pandas as pd
import struct
from contextlib import contextmanager
from functools import cached_property
from typing import Generator, List, Optional
from urllib.parse import quote_plus
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter

import pyodbc
from app.data_sources.fk_reflection import attach_foreign_keys
from app.data_sources.clients.mssql_client import _odbc_value

# ODBC driver-manager pooling would hold server sessions open after close(),
# under SQLAlchemy's pool; engine_pool disables it too, but this module dials
# pyodbc directly so it must not depend on import order.
pyodbc.pooling = False
from azure.identity import ClientSecretCredential

logger = logging.getLogger(__name__)

# Fabric Warehouse/Lakehouse SQL endpoints are serverless and can be slow to
# respond on the first connection after the capacity has been idle (cold start),
# routinely exceeding the ODBC driver's short default login timeout (~15s). Give
# the login a generous window and retry a couple of times on transient
# connection timeouts so a cold endpoint gets a chance to wake up.
_LOGIN_TIMEOUT_SECONDS = 60
_CONNECT_MAX_ATTEMPTS = 3
_CONNECT_RETRY_BACKOFF_SECONDS = 3
# ODBC SQLSTATEs that indicate a transient connection-level failure worth
# retrying (HYT00 = login timeout expired, HYT01 = connection timeout,
# 08001 = unable to establish connection, 08S01 = communication link failure).
_TRANSIENT_SQLSTATES = {"HYT00", "HYT01", "08001", "08S01"}


class MsFabricClient(DataSourceClient):
    """Client for Microsoft Fabric Warehouse/Lakehouse SQL endpoints."""

    # Fabric T-SQL: '' only (sqlglot fabric).
    sql_literal_style = "standard"

    # Rendered into codegen prompts (<connection_clients>) so generated queries
    # express time windows with the engine's own relative date functions instead
    # of literal dates that go stale when saved code is re-executed.
    relative_date_hint = "Relative dates (T-SQL): CAST(GETDATE() AS date), DATEADD(day, -7, CAST(GETDATE() AS date)), DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1) for month start; the clock is UTC."

    def __init__(
        self,
        server_hostname: str,
        database: str,
        tenant_id: str = None,
        client_id: str = None,
        client_secret: str = None,
        schema: Optional[str] = None,
        access_token: str = None,
    ):
        self.server_hostname = server_hostname
        self.database = database
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.schema = schema
        self._delegated_access_token = access_token
        self._company_reader = None

        # Parse comma-separated schemas if provided
        self._schemas: List[str] = []
        if isinstance(self.schema, str) and self.schema.strip():
            parts = [s.strip() for s in self.schema.split(",") if s.strip()]
            # Dedupe while preserving order
            seen = set()
            for p in parts:
                if p not in seen:
                    seen.add(p)
                    self._schemas.append(p)

    def _get_access_token(self) -> str:
        """Get Azure AD access token for SQL endpoint."""
        if self._delegated_access_token:
            return self._delegated_access_token

        credential = ClientSecretCredential(
            tenant_id=self.tenant_id,
            client_id=self.client_id,
            client_secret=self.client_secret,
        )
        token = credential.get_token("https://database.windows.net/.default")
        return token.token

    def _get_token_struct(self) -> bytes:
        """Convert token to struct format required by pyodbc."""
        token = self._get_access_token()
        token_bytes = token.encode("utf-16-le")
        token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
        return token_struct

    # Fabric speaks T-SQL, so it reuses the SQL Server row bounding (TOP) and
    # cost estimator (SHOWPLAN_XML). Neither has been exercised against a live
    # Fabric endpoint — see UNVERIFIED_TYPES in custom_query_service.
    EXTRACTION_DIALECT = "mssql"

    @cached_property
    def extraction_engine(self):
        """A pooled SQLAlchemy engine over this client's own pyodbc connections.

        Custom-query extraction needs a SQLAlchemy connection for its
        server-side cursor, but Fabric authenticates with an Entra access token
        handed to the driver through ``attrs_before={1256: ...}``. A token
        cannot live in a URL, so the usual "build an engine from a URI" route
        is not available.

        ``creator=`` is the way through: SQLAlchemy calls back into
        ``_open_connection`` for every physical connection, so the token
        handshake, the cold-start retry loop and the login timeout all keep
        working exactly as they do for normal queries, while the pool, the
        streaming cursor and the dialect machinery sit on top.

        The URL is a placeholder that carries no credentials — with
        ``creator=`` set, SQLAlchemy never dials it. It still has to be unique
        per Fabric target, because the engine pool keys on it and two Fabric
        connections must not share one pool.
        """
        if self._company_reader is not None:
            raise ValueError("Extraction unavailable on a company-filtered connection")
        import sqlalchemy

        from app.data_sources.engine_pool import get_engine

        placeholder = (
            f"mssql+pyodbc://fabric/?odbc_connect="
            f"{quote_plus(str(self.server_hostname))}%2F{quote_plus(str(self.database))}"
        )
        # Keyed additionally by the identity the token is minted for, so a
        # delegated (per-user) connection never shares a pool with another
        # user's — the same hazard the SQL Server client documents for Kerberos.
        # JWT prefixes contain shared headers, not the user's identity. Hash
        # the complete credential so users and token/secret rotations cannot
        # reuse a pool whose creator captured another authentication context.
        credentials = (
            ["delegated", self._delegated_access_token]
            if self._delegated_access_token
            else ["service", self.tenant_id, self.client_id, self.client_secret]
        )
        identity = hashlib.sha256(json.dumps(credentials).encode()).hexdigest()
        return get_engine(
            placeholder,
            key_extra=f"fabric:{identity}",
            creator=self._open_connection,
            # A token has a finite lifetime and a pooled connection outliving
            # it fails on next use. Recycling well inside the usual ~60-90 min
            # Entra lifetime keeps that from surfacing mid-extraction.
            pool_recycle=1500,
        )

    @contextmanager
    def extraction_connect(self):
        """Yield a SQLAlchemy Connection for extraction (see extraction_engine)."""
        with self.extraction_engine.connect() as conn:
            yield conn

    def _open_connection(self) -> "pyodbc.Connection":
        """Open a pyodbc connection to Fabric, retrying transient timeouts.

        Fabric endpoints can cold-start, so a generous login timeout plus a
        small retry loop avoids spurious ``HYT00 Login timeout expired`` errors
        while the serverless endpoint wakes up.
        """
        # Build connection string for Fabric.
        # Encrypt=yes / TrustServerCertificate=no are required for the Azure AD
        # auth handshake; Connect Timeout and ConnectRetryCount give the login
        # extra breathing room against a cold-starting endpoint.
        conn_str = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={_odbc_value(self.server_hostname)};"
            f"DATABASE={_odbc_value(self.database)};"
            f"Encrypt=yes;"
            f"TrustServerCertificate=no;"
            f"Connect Timeout={_LOGIN_TIMEOUT_SECONDS};"
            f"ConnectRetryCount=4;"
        )

        # Get token and pass via attrs_before
        token_struct = self._get_token_struct()

        last_error: Optional[Exception] = None
        for attempt in range(1, _CONNECT_MAX_ATTEMPTS + 1):
            try:
                # SQL_COPT_SS_ACCESS_TOKEN = 1256
                return pyodbc.connect(
                    conn_str,
                    attrs_before={1256: token_struct},
                    timeout=_LOGIN_TIMEOUT_SECONDS,
                )
            except pyodbc.Error as e:
                sqlstate = e.args[0] if e.args else None
                last_error = e
                if sqlstate in _TRANSIENT_SQLSTATES and attempt < _CONNECT_MAX_ATTEMPTS:
                    logger.warning(
                        "Fabric connect attempt %d/%d failed (%s); retrying in %ds",
                        attempt,
                        _CONNECT_MAX_ATTEMPTS,
                        sqlstate,
                        _CONNECT_RETRY_BACKOFF_SECONDS,
                    )
                    time.sleep(_CONNECT_RETRY_BACKOFF_SECONDS)
                    continue
                raise
        # Defensive: loop always returns or raises, but keep mypy/readers happy.
        raise last_error  # type: ignore[misc]

    @contextmanager
    def connect(self) -> Generator:
        """Yield a connection to Microsoft Fabric SQL endpoint."""
        if self._company_reader is not None:
            raise ValueError("A raw connection is unavailable for a company-filtered reader")
        with self._session() as conn:
            yield conn

    @contextmanager
    def _session(self):
        conn = None
        try:
            conn = self._open_connection()
            yield conn
        except IndexingCancelled:
            raise
        except Exception as e:
            raise RuntimeError(f"Error connecting to Microsoft Fabric: {e}")
        finally:
            if conn is not None:
                conn.close()

    @property
    def company_filtered(self):
        return self._company_reader is not None

    def bind_company_reader(self, policy, employee, identity=None):
        if self.company_filtered:
            raise ValueError("Company reader already bound")
        self._company_reader = (policy, employee, identity)

    def validate_company_policy(self, policy):
        """Check the exact catalog objects and comparable key types before publishing a mapping."""
        expected = [rule.table for rule in policy.tables]
        if policy.grants_table:
            expected.append(policy.grants_table)
        catalog = {}
        try:
            with self._session() as conn:
                cursor = conn.cursor()
                try:
                    for full_name in expected:
                        schema, name = full_name.split(".")
                        cursor.execute("""
                            SELECT c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE
                            FROM INFORMATION_SCHEMA.COLUMNS c JOIN INFORMATION_SCHEMA.TABLES t
                              ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
                            WHERE t.TABLE_TYPE = 'BASE TABLE' AND c.TABLE_SCHEMA = ? AND c.TABLE_NAME = ?
                        """, schema, name)
                        catalog[full_name] = {column: dtype.lower() for s, n, column, dtype in cursor.fetchall()
                                              if s == schema and n == name}
                finally:
                    cursor.close()
            for rule in policy.tables:
                if rule.columns is not None and set(rule.columns).difference(catalog[rule.table]):
                    raise ValueError("Unknown projected column")
                if rule.company_column not in catalog[rule.table]:
                    raise ValueError("Invalid row policy column")
            if not any(r.source for r in policy.tables):
                return
            grants = catalog[policy.grants_table]
            identifier_types = {"tinyint", "smallint", "int", "bigint", "uniqueidentifier"}
            text_types = {"nvarchar", "varchar"}
            if (grants.get(policy.employee_column) not in text_types
                    or grants.get(policy.source_column) not in text_types
                    or grants.get(policy.company_column) not in identifier_types
                    or any(catalog[r.table].get(r.company_column) != grants[policy.company_column]
                           for r in policy.tables if r.source)):
                raise ValueError("Incompatible grant schema")
        except Exception:
            raise ValueError("The configured base tables and grant columns could not be verified") from None

    def has_company_grant(self, rule):
        from app.data_sources.fabric_company_rls import protect_query
        from app.data_sources.fast.rls import resolve_source
        policy, employee, identity = self._company_reader
        if rule.row_attribute:
            return bool(resolve_source(rule.row_attribute, identity))
        statement, bindings = protect_query(f"SELECT * FROM {rule.table}", policy, employee,
            identity=identity, dialect=getattr(self, "policy_dialect", "fabric"), grant_probe=True)
        with self._session() as conn:
            cursor = conn.cursor()
            try:
                return bool(cursor.execute(statement, *bindings).fetchone()[0])
            finally:
                cursor.close()

    def execute_company_query(self, sql, policy, employee, identity=None):
        """Host-side execution. The authenticated caller supplies the binding."""
        from app.data_sources.fabric_company_rls import protect_query

        from app.data_sources.policy_history import record_policy
        record_policy(policy)
        try:
            if self.company_filtered and (policy, employee, identity) != self._company_reader:
                raise ValueError("Company reader mismatch")
            statement, bindings = protect_query(sql, policy, employee, identity=identity)
            with self._session() as conn:
                cursor = conn.cursor()
                try:
                    record_policy(policy, statement)
                    cursor.execute(statement, *bindings)
                    columns = [desc[0] for desc in cursor.description]
                    return pd.DataFrame.from_records(cursor.fetchall(), columns=columns)
                finally:
                    cursor.close()
        except Exception:
            # Driver and parser diagnostics may contain source values.
            raise ValueError("Company-filtered query unavailable") from None

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute SQL statement and return the result as a DataFrame."""
        if self.company_filtered:
            return self.execute_company_query(sql, *self._company_reader)
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute(sql)
                # Fetch column names from cursor description
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                rows = cursor.fetchall()
                cursor.close()
                df = pd.DataFrame.from_records(rows, columns=columns)
            return df
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
        """Get tables with column/table descriptions. May fail on some configurations."""
        discovery_phase('reading_columns')
        tables = {}
        with self.connect() as conn:
            cursor = conn.cursor()

            where_clauses = [f"c.TABLE_CATALOG = '{self.database}'"]
            where_clauses.append("c.TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'queryinsights')")
            # Filter to objects the connecting principal actually has SELECT on.
            # INFORMATION_SCHEMA shows objects whose existence is visible (e.g. via REFERENCES,
            # CONTROL, or schema membership), which is broader than SELECT — so a user with
            # DENY SELECT still sees the table listed without this filter.
            where_clauses.append(
                "HAS_PERMS_BY_NAME(QUOTENAME(c.TABLE_SCHEMA) + '.' + QUOTENAME(c.TABLE_NAME), 'OBJECT', 'SELECT') = 1"
            )
            if self._schemas:
                schema_list = ", ".join([f"'{s}'" for s in self._schemas])
                where_clauses.append(f"c.TABLE_SCHEMA IN ({schema_list})")

            where_sql = " WHERE " + " AND ".join(where_clauses)

            # Fabric supports extended properties for descriptions
            sql = f"""
                SELECT
                    c.TABLE_SCHEMA,
                    c.TABLE_NAME,
                    c.COLUMN_NAME,
                    c.DATA_TYPE,
                    CAST(ep_col.value AS NVARCHAR(MAX)) AS column_comment,
                    CAST(ep_tbl.value AS NVARCHAR(MAX)) AS table_comment
                FROM INFORMATION_SCHEMA.COLUMNS c
                LEFT JOIN sys.columns sc
                    ON sc.name = c.COLUMN_NAME
                    AND sc.object_id = OBJECT_ID(c.TABLE_SCHEMA + '.' + c.TABLE_NAME)
                LEFT JOIN sys.extended_properties ep_col
                    ON ep_col.major_id = sc.object_id
                    AND ep_col.minor_id = sc.column_id
                    AND ep_col.name = 'MS_Description'
                LEFT JOIN sys.extended_properties ep_tbl
                    ON ep_tbl.major_id = OBJECT_ID(c.TABLE_SCHEMA + '.' + c.TABLE_NAME)
                    AND ep_tbl.minor_id = 0
                    AND ep_tbl.name = 'MS_Description'
                {where_sql}
                ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION
            """

            cursor.execute(sql)
            results = cursor.fetchall()
            cursor.close()

            for row in discovery_items(results, 'columns', label=lambda row: '.'.join(str(v) for v in row[:3])):
                table_schema, table_name, column_name, data_type, col_comment, tbl_comment = row
                key = (table_schema, table_name)
                fqn = f"{table_schema}.{table_name}"
                if key not in tables:
                    tables[key] = Table(
                        name=fqn,
                        description=tbl_comment if tbl_comment else None,
                        columns=[],
                        pks=[],
                        fks=[],
                        metadata_json={"schema": table_schema, "database": self.database}
                    )
                tables[key].columns.append(TableColumn(
                    name=column_name,
                    dtype=data_type,
                    description=col_comment if col_comment else None
                ))

        self._attach_foreign_keys(tables)
        return list(tables.values())

    def _get_tables_basic(self) -> List[Table]:
        """Get tables without comments (always works)."""
        discovery_phase('metadata_fallback')
        tables = {}
        with self.connect() as conn:
            cursor = conn.cursor()

            where_clauses = [f"TABLE_CATALOG = '{self.database}'"]
            where_clauses.append("TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'queryinsights')")
            # Filter to objects the connecting principal actually has SELECT on
            # (see _get_tables_enriched for rationale).
            where_clauses.append(
                "HAS_PERMS_BY_NAME(QUOTENAME(TABLE_SCHEMA) + '.' + QUOTENAME(TABLE_NAME), 'OBJECT', 'SELECT') = 1"
            )
            if self._schemas:
                schema_list = ", ".join([f"'{s}'" for s in self._schemas])
                where_clauses.append(f"TABLE_SCHEMA IN ({schema_list})")

            where_sql = " WHERE " + " AND ".join(where_clauses)

            sql = f"""
                SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS
                {where_sql}
                ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
            """

            cursor.execute(sql)
            results = cursor.fetchall()
            cursor.close()

            for row in discovery_items(results, 'columns', label=lambda row: '.'.join(str(v) for v in row[:3])):
                table_schema, table_name, column_name, data_type = row
                key = (table_schema, table_name)
                fqn = f"{table_schema}.{table_name}"
                if key not in tables:
                    tables[key] = Table(
                        name=fqn,
                        columns=[],
                        pks=[],
                        fks=[],
                        metadata_json={"schema": table_schema, "database": self.database}
                    )
                tables[key].columns.append(TableColumn(name=column_name, dtype=data_type))

        self._attach_foreign_keys(tables)
        return list(tables.values())

    def get_schema(self, table_name: str) -> Table:
        """Get schema for a specific table. Deprecated - use get_tables() instead."""
        raise NotImplementedError("get_schema() is deprecated. Use get_tables() instead.")

    def _attach_foreign_keys(self, tables) -> None:
        """Populate `fks` on the tables just collected.

        `connect()` yields a raw pyodbc connection (Fabric authenticates with an
        Entra token passed through `attrs_before`, which no URL can carry), and
        `sqlalchemy.inspect` cannot drive one. Reflection therefore goes through
        `extraction_engine`, the pooled SQLAlchemy handle already built over the
        same `_open_connection` callback — so the token handshake and cold-start
        retry apply here exactly as they do to a query.

        Fabric declares foreign keys but does not enforce them, so most
        warehouses have none to find; this returns whatever was declared.

        `name_fn` mirrors the `f"{table_schema}.{table_name}"` used for
        `Table.name` — the two must agree or downstream resolution, an exact
        string match, drops every edge without complaining.
        """
        if not tables:
            return
        try:
            with self.extraction_engine.connect() as conn:
                attach_foreign_keys(
                    conn,
                    tables,
                    None,
                    lambda schema, table: f"{schema}.{table}",
                )
        except IndexingCancelled:
            raise
        except Exception:
            logger.debug("Fabric FK reflection failed; continuing", exc_info=True)

    @discovery_progress
    def get_schemas(self, progress_callback=None) -> List[Table]:
        """Get all table schemas. Wrapper for get_tables()."""
        return self.get_tables()

    def prompt_schema(self) -> str:
        """Return formatted schema string for LLM prompts."""
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self) -> dict:
        """Test connection to Microsoft Fabric and return status information."""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                cursor.close()
                return {
                    "success": True,
                    "message": "Successfully connected to Microsoft Fabric"
                }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @property
    def description(self) -> str:
        """System prompt describing this data source for LLM context."""
        schema_info = ", ".join(self._schemas) if self._schemas else "all schemas"
        return f"""Microsoft Fabric SQL Endpoint
Server: {self.server_hostname}
Database: {self.database}
Schemas: {schema_info}

Microsoft Fabric uses T-SQL syntax (SQL Server compatible).
Tables are organized in a two-level namespace: schema.table

T-SQL syntax rules:
- Use TOP N instead of LIMIT (e.g., SELECT TOP 10 * FROM table)
- String concatenation uses + not ||
- Use GETDATE() instead of NOW()
- Use ISNULL() or COALESCE() for null handling
- Use DATEPART(), DATEADD(), DATEDIFF() for date operations
- When using UNION/INTERSECT/EXCEPT with ORDER BY, the ORDER BY column must appear in the SELECT list
- Use square brackets [column] for reserved words or special characters
- Use CAST(x AS VARCHAR) or CONVERT() for type conversions
"""
