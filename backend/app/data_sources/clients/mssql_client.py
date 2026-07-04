from app.data_sources.clients.base import DataSourceClient

import logging
import pandas as pd
import sqlalchemy
from sqlalchemy import text
from contextlib import contextmanager
from typing import Generator, List, Optional
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property

logger = logging.getLogger(__name__)


class MSSQLClient(DataSourceClient):
    SUPPORTED_ODBC_DRIVERS = {17, 18}
    # ODBC keywords the client owns; user-supplied additional params can never
    # override these (case-insensitive), so the escape hatch can't weaken TLS,
    # repoint the driver, swap credentials, or flip the auth scheme.
    PROTECTED_ODBC_KEYS = {
        "driver", "server", "database", "uid", "pwd",
        "encrypt", "trustservercertificate",
        "trusted_connection", "authentication", "integrated security",
        # "app" carries the per-identity pool discriminator under Kerberos SSO;
        # letting a user override it could re-open the cross-user pooling hole.
        "app",
    }

    def __init__(self, host, port, database, user=None, password=None, schema: Optional[str] = None,
                 odbc_driver: int = 18, encrypt: bool = True, additional_params: Optional[dict] = None,
                 use_kerberos: bool = False, kerberos_principal: Optional[str] = None,
                 kerberos_impersonate: Optional[str] = None):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.schema = schema
        self.odbc_driver = int(odbc_driver)
        if self.odbc_driver not in self.SUPPORTED_ODBC_DRIVERS:
            raise ValueError(f"Unsupported ODBC driver version: {self.odbc_driver}. Supported: {sorted(self.SUPPORTED_ODBC_DRIVERS)}")
        self.encrypt = encrypt
        self.additional_params = additional_params or {}
        # Kerberos / Windows Integrated auth. The driver derives the target SPN
        # as MSSQLSvc/<host>:<port> — `host` must be the FQDN, not an IP.
        #   - use_kerberos alone: default credential cache (service keytab).
        #   - kerberos_principal: initiate as this principal from the client keytab.
        #   - kerberos_impersonate: per-user constrained delegation (S4U) — the
        #     app impersonates this UPN and queries as that user.
        self.use_kerberos = bool(use_kerberos)
        self.kerberos_principal = (kerberos_principal or "").strip() or None
        self.kerberos_impersonate = (kerberos_impersonate or "").strip() or None
        self._schemas = []
        if isinstance(self.schema, str) and self.schema.strip():
            parts = [s.strip() for s in self.schema.split(",") if s.strip()]
            seen = set()
            for p in parts:
                if p not in seen:
                    seen.add(p)
                    self._schemas.append(p)

    @cached_property
    def sql_server_uri(self):
        from urllib.parse import quote_plus
        driver_name = f"ODBC Driver {self.odbc_driver} for SQL Server"
        params = (
            f"DRIVER={driver_name};"
            f"SERVER={self.host},{self.port};"
            f"DATABASE={self.database};"
        )
        if self.use_kerberos:
            # Integrated auth: the driver authenticates via GSSAPI from the
            # credential cache active at connect time. No NTLM fallback exists
            # on Linux, and the SPN cannot be overridden (always MSSQLSvc/host:port).
            params += "Trusted_Connection=yes;"
            # SECURITY: under integrated auth the identity comes from KRB5CCNAME
            # at connect time, NOT from the connection string — so every user's
            # string is otherwise identical. ODBC driver-manager connection
            # pooling keys on the string, so without a per-identity discriminator
            # user B can be handed a pooled connection still authenticated as
            # user A. Bind the app-name to the impersonated principal so each
            # identity gets its own pool bucket (verified in the Kerberos lab).
            ident = self.kerberos_impersonate or self.kerberos_principal or "service"
            safe_ident = "".join(c if c.isalnum() or c in "@._-" else "_" for c in ident)[:96]
            params += f"APP=BagOfWords-{safe_ident};"
        else:
            params += (
                f"UID={self.user};"
                f"PWD={self.password};"
            )
        params += (
            "TrustServerCertificate=yes;"
            "LoginTimeout=30;"
        )
        if not self.encrypt:
            params += "Encrypt=no;"
        # Append user-supplied extra ODBC keywords (e.g. ApplicationIntent=ReadOnly),
        # skipping any that would override the security-sensitive keys above.
        for key, value in (self.additional_params or {}).items():
            k = str(key).strip()
            if not k or k.lower() in self.PROTECTED_ODBC_KEYS:
                continue
            params += f"{k}={value};"
        return f"mssql+pyodbc:///?odbc_connect={quote_plus(params)}"

    def _kerberos_ccache(self) -> Optional[str]:
        """Resolve the credential cache to use for this connection, if any."""
        if not self.use_kerberos:
            return None
        from app.data_sources.kerberos import get_ticket_manager
        manager = get_ticket_manager()
        if self.kerberos_impersonate:
            return manager.delegated_ccache(self.kerberos_impersonate)
        return manager.service_ccache(self.kerberos_principal)

    @contextmanager
    def connect(self) -> Generator[sqlalchemy.engine.base.Connection, None, None]:
        """Yield a connection to a SQL Server database."""
        engine = None
        conn = None
        try:
            if self.use_kerberos:
                from app.data_sources.kerberos import get_ticket_manager
                ccache = self._kerberos_ccache()
                engine = sqlalchemy.create_engine(self.sql_server_uri)
                # KRB5CCNAME is process-global; hold the activation lock only
                # while the driver performs the GSS handshake. The established
                # connection stays bound to its identity afterwards.
                with get_ticket_manager().activate(ccache):
                    conn = engine.connect()
            else:
                engine = sqlalchemy.create_engine(self.sql_server_uri)
                conn = engine.connect()
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
            logger.exception("MSSQL enriched table introspection failed; falling back to basic query")
            return self._get_tables_basic()

    def _get_tables_enriched(self) -> List[Table]:
        """Get tables with column/table descriptions via extended properties. May fail on some configurations."""
        with self.connect() as conn:
            params = {"database": self.database}
            where_clauses = ["c.table_catalog = :database"]

            if self._schemas:
                in_keys = []
                for idx, sch in enumerate(self._schemas):
                    key = f"s{idx}"
                    params[key] = sch
                    in_keys.append(f":{key}")
                where_clauses.append(f"c.table_schema IN ({', '.join(in_keys)})")

            where_sql = " AND ".join(where_clauses)
            sql = text(f"""
                SELECT
                    c.table_schema,
                    c.table_name,
                    c.column_name,
                    c.data_type,
                    CAST(ep_col.value AS NVARCHAR(MAX)) AS column_comment,
                    CAST(ep_tbl.value AS NVARCHAR(MAX)) AS table_comment
                FROM INFORMATION_SCHEMA.COLUMNS c
                LEFT JOIN sys.columns sc
                    ON sc.name = c.column_name
                    AND sc.object_id = OBJECT_ID(c.table_schema + '.' + c.table_name)
                LEFT JOIN sys.extended_properties ep_col
                    ON ep_col.major_id = sc.object_id
                    AND ep_col.minor_id = sc.column_id
                    AND ep_col.name = 'MS_Description'
                LEFT JOIN sys.extended_properties ep_tbl
                    ON ep_tbl.major_id = OBJECT_ID(c.table_schema + '.' + c.table_name)
                    AND ep_tbl.minor_id = 0
                    AND ep_tbl.name = 'MS_Description'
                WHERE {where_sql}
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
                        description=tbl_comment if tbl_comment else None,
                        columns=[],
                        pks=None,
                        fks=None,
                        metadata_json={"schema": table_schema}
                    )
                tables[key].columns.append(TableColumn(
                    name=column_name,
                    dtype=data_type,
                    description=col_comment if col_comment else None
                ))
            return list(tables.values())

    def _get_tables_basic(self) -> List[Table]:
        """Get tables without comments (original query - always works)."""
        with self.connect() as conn:
            params = {"database": self.database}
            where_clauses = ["table_catalog = :database"]

            if self._schemas:
                in_keys = []
                for idx, sch in enumerate(self._schemas):
                    key = f"s{idx}"
                    params[key] = sch
                    in_keys.append(f":{key}")
                where_clauses.append(f"table_schema IN ({', '.join(in_keys)})")

            where_sql = " AND ".join(where_clauses)
            sql = text(f"""
                SELECT table_schema, table_name, column_name, data_type
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE {where_sql}
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
                        name=fqn, columns=[], pks=None, fks=None,
                        metadata_json={"schema": table_schema})
                tables[key].columns.append(
                    TableColumn(name=column_name, dtype=data_type))
            return list(tables.values())

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
        """Test connection to SQL Server and return status information."""
        try:
            with self.connect() as conn:
                conn.execute(text("SELECT 1"))
                return {
                    "success": True,
                    "message": "Successfully connected to SQL Server"
                }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @property
    def description(self):
        description = f"SQL Server client for database '{self.database}' at {self.host}:{self.port}"
        return description
