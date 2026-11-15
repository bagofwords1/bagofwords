from app.data_sources.clients.progress import discovery_progress, discovery_items, discovery_phase, IndexingCancelled
from app.data_sources.clients.base import DataSourceClient

import logging
import re
import pandas as pd
import sqlalchemy

from app.data_sources.engine_pool import get_engine
from app.data_sources.fk_reflection import attach_foreign_keys
from app.data_sources.query_cancellation import capture_identity, track
from sqlalchemy import text
from contextlib import contextmanager
from typing import Generator, List, Optional, Tuple
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property
from app.data_sources.routines import build_routine_tables, mssql_variants, with_routines

logger = logging.getLogger(__name__)


def _odbc_value(value):
    """Keep delimiters inside a value rather than interpreting new keywords."""
    value = str(value)
    if "\x00" in value:
        raise ValueError("ODBC values cannot contain NUL")
    if any(c in value for c in ";{}") or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


# Isolation level choices for the connection config -> the T-SQL name.
# "default" keeps READ COMMITTED, the level every SQL Server session starts
# in; with READ_COMMITTED_SNAPSHOT on in the database that is already a
# lock-free read, so it stays the default. READ UNCOMMITTED reads rows other
# transactions have not committed (dirty reads) and must be an admin's
# explicit choice.
ISOLATION_LEVELS = {
    "default": "READ COMMITTED",
    "snapshot": "SNAPSHOT",
    "read_uncommitted": "READ UNCOMMITTED",
}

# How long a BOW query waits for a lock before failing with error 1222.
# Shorter than the default 60s query timeout, so a blocked query fails on its
# own (and releases its schema lock) instead of being timed out and KILLed.
DEFAULT_LOCK_TIMEOUT_SECONDS = 15

# SQL Server: "User does not have permission to use the KILL statement."
_KILL_PERMISSION_HINT = (
    "This login lacks the ALTER ANY CONNECTION permission, so BOW cannot stop "
    "a query on SQL Server after it times out: the query keeps running and "
    "holding locks until it finishes. Grant ALTER ANY CONNECTION to the login "
    "to let BOW stop timed-out queries."
)
# Azure SQL Database has no server-level permissions; KILL there needs the
# database permission KILL DATABASE CONNECTION.
_KILL_PERMISSION_HINT_AZURE = (
    "This user lacks the KILL DATABASE CONNECTION permission, so BOW cannot "
    "stop a query on Azure SQL Database after it times out: the query keeps "
    "running and holding locks until it finishes. Grant KILL DATABASE "
    "CONNECTION to the user to let BOW stop timed-out queries."
)
# SERVERPROPERTY('EngineEdition') of Azure SQL Database. Managed Instance (8)
# and on-premises SQL Server keep server-level permissions.
_AZURE_SQL_DATABASE_EDITION = 5


class MssqlLockConflictError(RuntimeError):
    """A query SQL Server stopped so the database's own writers could proceed.

    Raised for error 1222 (lock timeout) and for a deadlock still lost after
    the one retry. The message says what happened and what to do, instead of
    the driver's text. The driver error stays chained as ``__cause__``.
    """


def _sql_server_error_code(e: BaseException) -> Optional[int]:
    """The SQL Server error number in a driver error, for the few we map.

    pyodbc puts it in the message as "(1222)" before "(SQLExecDirectW)".
    """
    text = str(e)
    for code in (1205, 1222, 3951, 3952):
        if f"({code})" in text:
            return code
    return None


def _is_snapshot_refusal(e: BaseException) -> bool:
    """SQL Server 3952: "Snapshot isolation transaction failed accessing
    database ... because snapshot isolation is not allowed in this database"."""
    text = str(e).lower()
    return "(3952)" in text or "snapshot isolation is not allowed" in text


def _session_setup_hook(sql: str):
    """Engine `connect` hook that runs `sql` on each new physical session.

    It runs before SQLAlchemy uses the connection (pre-ping included), so no
    transaction is open yet — SNAPSHOT cannot be entered mid-transaction. A
    failure here fails the connection: a session without these settings is
    exactly what this exists to prevent. It closes over the SQL only, not
    the client, because the engine outlives any one client instance.
    """
    def hook(dbapi_connection, _connection_record):
        try:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(sql)
            finally:
                cursor.close()
        except BaseException:
            # SQLAlchemy discards the pool record when a connect listener
            # raises, but does not close the driver connection: without this
            # it stays open on the server until garbage collection.
            try:
                dbapi_connection.close()
            except Exception:
                pass
            raise
    return hook


class MSSQLClient(DataSourceClient):

    # T-SQL: '' only, backslash is ordinary (sqlglot tsql).
    sql_literal_style = "standard"

    # Rendered into codegen prompts (<connection_clients>) so generated queries
    # express time windows with the engine's own relative date functions instead
    # of literal dates that go stale when saved code is re-executed.
    relative_date_hint = "Relative dates (T-SQL): CAST(GETDATE() AS date), DATEADD(day, -7, CAST(GETDATE() AS date)), DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1) for month start; the clock is the DB server's."

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
                 kerberos_impersonate: Optional[str] = None,
                 kerberos_expected_sid: Optional[str] = None,
                 index_routines: bool = False,
                 deadlock_priority_low: bool = True,
                 lock_timeout_seconds: Optional[int] = DEFAULT_LOCK_TIMEOUT_SECONDS,
                 isolation_level: Optional[str] = "default"):
        self.host = host
        # BOW is a reader on somebody else's database. These are applied to
        # every new physical connection (see `session_setup_sql`).
        self.deadlock_priority_low = bool(deadlock_priority_low)
        if lock_timeout_seconds is None:
            lock_timeout_seconds = DEFAULT_LOCK_TIMEOUT_SECONDS
        self.lock_timeout_seconds = int(lock_timeout_seconds)
        if self.lock_timeout_seconds < 0:
            raise ValueError("Lock timeout cannot be negative")
        self.isolation_level = (isolation_level or "default").strip().lower()
        if self.isolation_level not in ISOLATION_LEVELS:
            raise ValueError(
                f"Unsupported isolation level: {isolation_level}. "
                f"Supported: {sorted(ISOLATION_LEVELS)}"
            )
        # Opt-in routine indexing (see app.data_sources.routines).
        self.index_routines = bool(index_routines)
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
        self.kerberos_expected_sid = kerberos_expected_sid
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
            f"SERVER={_odbc_value(f'{self.host},{self.port}')};"
            f"DATABASE={_odbc_value(self.database)};"
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
                f"UID={_odbc_value(self.user)};"
                f"PWD={_odbc_value(self.password)};"
                # Names BOW's sessions in sys.dm_exec_sessions and in deadlock
                # graphs, so a DBA can tell them apart from the application's.
                "APP=BagOfWords;"
            )
        params += (
            "TrustServerCertificate=no;"
            "LoginTimeout=30;"
            f"Encrypt={'yes' if self.encrypt else 'no'};"
        )
        # Append user-supplied extra ODBC keywords (e.g. ApplicationIntent=ReadOnly),
        # skipping any that would override the security-sensitive keys above.
        for key, value in (self.additional_params or {}).items():
            k = str(key).strip()
            if not k or k.lower() in self.PROTECTED_ODBC_KEYS:
                continue
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 _]*", k):
                raise ValueError("Invalid ODBC keyword")
            params += f"{k}={_odbc_value(value)};"
        return f"mssql+pyodbc:///?odbc_connect={quote_plus(params)}"

    @property
    def session_setup_sql(self) -> str:
        """The per-session SET batch run on every new physical connection.

        SQL Server readers take shared locks under READ COMMITTED (unless the
        database has READ_COMMITTED_SNAPSHOT on), so a BOW query can deadlock
        with the customer's writers or block them. Three settings make BOW the
        side that gives way:

          * DEADLOCK_PRIORITY LOW — in a deadlock SQL Server kills BOW's
            query (error 1205), not the customer's transaction. The agent can
            retry; their application usually cannot.
          * LOCK_TIMEOUT — a blocked BOW query fails with error 1222 after
            this long instead of waiting forever, and so stops holding the
            schema lock that queues DDL (and everyone behind it).
          * TRANSACTION ISOLATION LEVEL — the admin's choice; see
            ISOLATION_LEVELS.

        SET options belong to the session and survive pool check-in (ODBC
        driver-manager pooling, whose sp_reset_connection would clear them,
        is off — engine_pool), so once per physical connection is enough.
        Every setting is written out even at its server default, so the batch
        fully describes the session.
        """
        lock_ms = self.lock_timeout_seconds * 1000 if self.lock_timeout_seconds > 0 else -1
        return "\n".join((
            f"SET DEADLOCK_PRIORITY {'LOW' if self.deadlock_priority_low else 'NORMAL'};",
            f"SET LOCK_TIMEOUT {int(lock_ms)};",
            f"SET TRANSACTION ISOLATION LEVEL {ISOLATION_LEVELS[self.isolation_level]};",
        ))

    def _get_engine(self, ccache: Optional[str] = None):
        # The hook's SQL depends on this client's settings, so it is part of
        # the pool key: two connections to one server with different lock
        # settings must not share pooled sessions.
        sql = self.session_setup_sql
        key = sql if ccache is None else f"{ccache}|{sql}"
        return get_engine(self.sql_server_uri, key_extra=key, on_connect=_session_setup_hook(sql))

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
        conn = None
        try:
            if self.kerberos_impersonate and not self.kerberos_expected_sid:
                raise RuntimeError("Delegation requires a verified directory SID")
            if self.use_kerberos:
                from app.data_sources.kerberos import get_ticket_manager
                ccache = self._kerberos_ccache()
                # A pooled connection stays bound to the identity that
                # performed its GSS handshake, so the pool is keyed by ccache —
                # sharing one across principals would hand out a connection
                # authenticated as somebody else.
                # Keep lazy engine/driver initialization and the handshake
                # under the same credential-cache selection. Initializing an
                # engine before activation can observe another thread's cache.
                # Established queries run outside this process-wide lock.
                with get_ticket_manager().activate(ccache):
                    engine = self._get_engine(str(ccache))
                    conn = engine.connect()
            else:
                engine = self._get_engine()
                conn = engine.connect()
            if self.kerberos_impersonate:
                self.verify_directory_identity(conn)
            # pyodbc has no connection-level cancel, so a timed-out query is
            # stopped with KILL <spid> from a side connection. The SPID is
            # server-side, so it has to be read while the connection is idle;
            # it is cached on the pooled connection, making this one round trip
            # per physical connection rather than per query.
            capture_identity(conn)
        except IndexingCancelled:
            raise
        except Exception as e:
            if conn is not None:
                if self.kerberos_impersonate:
                    conn.invalidate()
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

    def verify_directory_identity(self, conn):
        """Also used by the independent timeout-cancellation connection."""
        if not self.kerberos_impersonate:
            return
        try:
            row = conn.execute(text(
                "SELECT SUSER_SID(ORIGINAL_LOGIN()), "
                "CONVERT(varchar(30), CONNECTIONPROPERTY('auth_scheme'))"
            )).one()
            if (not self.kerberos_expected_sid or row[0] is None or
                    bytes(row[0]).hex().lower() != self.kerberos_expected_sid.lower()
                    or str(row[1]).upper() != "KERBEROS"):
                raise RuntimeError("SQL authenticated identity mismatch")
        except IndexingCancelled:
            raise
        except Exception:
            conn.invalidate()
            raise

    # One retry for a lost deadlock: SQL Server rolled the victim back
    # entirely, so running the statement again is the same as running it the
    # first time, and the other transaction has usually finished by then.
    DEADLOCK_RETRIES = 1

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute SQL statement and return the result as a DataFrame."""
        attempt = 0
        while True:
            try:
                with self.connect() as conn:
                    df = pd.read_sql(text(sql), conn)
                return df
            except Exception as e:
                code = _sql_server_error_code(e)
                if code == 1205 and attempt < self.DEADLOCK_RETRIES:
                    attempt += 1
                    logger.info("SQL Server chose this query as a deadlock victim; retrying once")
                    continue
                mapped = self._explain_server_error(e, code)
                if mapped is not None:
                    raise mapped from e
                print(f"Error executing SQL: {e}")
                raise

    def _explain_server_error(self, e: BaseException, code: Optional[int]) -> Optional[Exception]:
        """A clear error for the failures BOW's own settings produce, or None.

        With DEADLOCK_PRIORITY LOW and a lock timeout, BOW now gives way on
        purpose. The driver's text ("Lock request time out period exceeded.")
        says neither that this was deliberate nor what to do next.
        """
        if code == 1222:
            limit = f"{self.lock_timeout_seconds}s" if self.lock_timeout_seconds > 0 else "its limit"
            return MssqlLockConflictError(
                "SQL Server error 1222: rows this query reads are locked by another "
                f"session, and the query gave up after waiting {limit} so it would not "
                "block the database's own work. Retry later, or read fewer rows (a "
                "narrower filter or TOP n). An admin can change the wait in the "
                "connection's Lock Timeout setting."
            )
        if code == 1205:
            return MssqlLockConflictError(
                "SQL Server error 1205: this query was chosen as a deadlock victim twice "
                "in a row, so the database's own transactions could finish. It was "
                "rolled back and changed nothing. Retry later, when the database is "
                "less busy."
            )
        # 3952 comes from the connection's own database; reading another
        # database that does not allow snapshot (a three-part name) is 3951,
        # verified on SQL Server 2022. This client enters SNAPSHOT before any
        # transaction starts, so 3951 cannot mean a mid-transaction switch.
        if code in (3951, 3952) or _is_snapshot_refusal(e):
            return MssqlLockConflictError(
                f"SQL Server error {code or 3952}: this connection uses snapshot isolation, but a "
                "database the query reads does not allow it. Run ALTER DATABASE "
                "<name> SET ALLOW_SNAPSHOT_ISOLATION ON on that database, or choose a "
                "different isolation level for this connection."
            )
        return None

    def get_tables(self) -> List[Table]:
        """Get tables with graceful fallback if enriched query fails."""
        try:
            return self._get_tables_enriched()
        except IndexingCancelled:
            raise
        except Exception:
            logger.exception("MSSQL enriched table introspection failed; falling back to basic query")
            return self._get_tables_basic()

    def _get_tables_enriched(self) -> List[Table]:
        """Get tables with column/table descriptions via extended properties. May fail on some configurations."""
        discovery_phase('reading_columns')
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
            for row in discovery_items(result, 'columns', label=lambda row: '.'.join(str(v) for v in row[:3])):
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
            self._attach_foreign_keys(conn, tables)
            return list(tables.values())

    def _get_tables_basic(self) -> List[Table]:
        """Get tables without comments (original query - always works)."""
        discovery_phase('metadata_fallback')
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
            for row in discovery_items(result, 'columns', label=lambda row: '.'.join(str(v) for v in row[:3])):
                table_schema, table_name, column_name, data_type = row
                key = (table_schema, table_name)
                fqn = f"{table_schema}.{table_name}"

                if key not in tables:
                    tables[key] = Table(
                        name=fqn, columns=[], pks=None, fks=None,
                        metadata_json={"schema": table_schema})
                tables[key].columns.append(
                    TableColumn(name=column_name, dtype=data_type))
            self._attach_foreign_keys(conn, tables)
            return list(tables.values())

    def _attach_foreign_keys(self, conn, tables) -> None:
        """Populate `fks` on the tables just collected.

        `name_fn` mirrors the `f"{table_schema}.{table_name}"` this client uses
        for `Table.name` — the two must agree or downstream resolution, which is
        an exact string match, drops every edge without complaining.
        """
        attach_foreign_keys(
            conn,
            tables,
            self._schemas or None,
            lambda schema, table: f"{schema}.{table}",
        )

    def get_schema(self, table_id: str) -> Table:
        """This method is now obsolete. Please use get_tables() instead."""
        raise NotImplementedError(
            "get_schema() is obsolete. Use get_tables() instead.")

    @discovery_progress
    def get_schemas(self, progress_callback=None):
        """Tables and views, then stored procedures and functions (see
        app.data_sources.routines)."""
        tables = self.get_tables()
        return with_routines(tables, lambda: self.get_routines(t.name for t in tables), source="mssql",
                             client=self, enabled=self.index_routines)

    def get_routines(self, table_names=()) -> List[Table]:
        """Stored procedures and functions as schema entries, named like tables."""
        with self.connect() as conn:
            variants = mssql_variants(conn, self._schemas or None)
        return build_routine_tables(variants, table_names, lambda schema, name: f"{schema}.{name}")

    def prompt_schema(self):
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        """Test connection to SQL Server and return status information.

        Beyond "can we log in", this checks the two things that decide
        whether BOW is a safe reader on this database: that the chosen
        isolation level can actually be used, and whether BOW will be able to
        stop its own timed-out queries. The first fails the test; the second
        is a warning, because the connection works without it.
        """
        try:
            with self.connect() as conn:
                conn.execute(text("SELECT 1"))
                if self.isolation_level == "snapshot":
                    problem = self._snapshot_problem(conn)
                    if problem:
                        return {"success": False, "message": problem}
                warnings = []
                can_kill, azure = self._can_kill_sessions(conn)
                if can_kill is False:
                    warnings.append(_KILL_PERMISSION_HINT_AZURE if azure else _KILL_PERMISSION_HINT)
                message = "Successfully connected to SQL Server"
                if warnings:
                    message += ". " + " ".join(warnings)
                result = {"success": True, "message": message}
                if warnings:
                    result["warnings"] = warnings
                return result
        except Exception as e:
            if self.isolation_level == "snapshot" and _is_snapshot_refusal(e):
                # The refusal can come while the session is still being set
                # up, before the explicit check below gets to run.
                return {"success": False, "message": self._snapshot_message()}
            return {
                "success": False,
                "message": str(e)
            }

    def _snapshot_problem(self, conn) -> Optional[str]:
        """Why SNAPSHOT isolation cannot be used here, or None if it can.

        SET TRANSACTION ISOLATION LEVEL SNAPSHOT succeeds anywhere; the
        failure (error 3952) only comes with the first query that reads the
        database. Catch that at test time with a message that names the fix,
        instead of on every agent query.
        """
        try:
            state = conn.execute(text(
                "SELECT snapshot_isolation_state FROM sys.databases WHERE database_id = DB_ID()"
            )).scalar()
        except Exception as e:
            if _is_snapshot_refusal(e):
                return self._snapshot_message()
            raise
        # 1 = ON. 0 = OFF, 2/3 = in transition (not usable yet).
        if state is not None and int(state) != 1:
            return self._snapshot_message()
        return None

    def _snapshot_message(self) -> str:
        return (
            f"Snapshot isolation is not enabled on database '{self.database}'. "
            f"Run ALTER DATABASE [{self.database}] SET ALLOW_SNAPSHOT_ISOLATION ON, "
            "or choose a different isolation level for this connection."
        )

    def _can_kill_sessions(self, conn) -> Tuple[Optional[bool], bool]:
        """(may this login KILL its own timed-out sessions, is this Azure SQL DB).

        The first value is True/False when the server answers and None when
        it cannot say. Azure SQL Database has no server-level permissions, so
        there the check is the database permission KILL DATABASE CONNECTION;
        asking it about ALTER ANY CONNECTION would recommend a grant that
        cannot be made. Never raises: this only decides whether to warn.
        """
        azure = False
        try:
            edition = conn.execute(text("SELECT CONVERT(int, SERVERPROPERTY('EngineEdition'))")).scalar()
            azure = edition is not None and int(edition) == _AZURE_SQL_DATABASE_EDITION
        except Exception as e:
            # Unknown edition: check as on-premises SQL Server.
            logger.debug("Could not read SQL Server edition: %s", e)
        try:
            if azure:
                value = conn.execute(text(
                    "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'KILL DATABASE CONNECTION')"
                )).scalar()
            else:
                value = conn.execute(text(
                    "SELECT HAS_PERMS_BY_NAME(NULL, NULL, 'ALTER ANY CONNECTION')"
                )).scalar()
        except Exception as e:
            logger.debug("Could not check KILL permission: %s", e)
            return None, azure
        if value is None:
            return None, azure
        return bool(int(value)), azure

    @property
    def description(self):
        description = f"SQL Server client for database '{self.database}' at {self.host}:{self.port}"
        return description
