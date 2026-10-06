from app.data_sources.clients.progress import discovery_progress, discovery_items, discovery_phase, IndexingCancelled
from app.data_sources.clients.base import DataSourceClient

import logging
import os
import ssl

import oracledb
import pandas as pd
import sqlalchemy

from app.data_sources.engine_pool import get_engine
from app.data_sources.query_cancellation import track, run_bounded, SourceCallTimeout
from sqlalchemy import text
from contextlib import contextmanager
from typing import Generator, List, Optional
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from app.data_sources.fk_reflection import attach_foreign_keys
from functools import cached_property

logger = logging.getLogger(__name__)


def init_thick_mode_if_available() -> bool:
    """Load Oracle Client libraries (thick mode) when they are installed.

    Called once at application startup, before any Oracle connection exists —
    the mode is process-wide and cannot change after the first connection.
    Thin mode (the pure-Python default) cannot talk to Oracle servers older
    than 12.1, to accounts with only a 10G password verifier (DPY-3015), or
    through Native Network Encryption (DPY-4011 "connection reset by peer").
    Thick mode supports all of those plus everything thin mode does, so it is
    preferred whenever the Instant Client libraries are present (bundled in
    the Docker image). Hosts without the libraries keep thin mode unchanged.

    Returns True when thick mode was enabled, False when staying thin.

    Set ORACLE_THICK_MODE=0 to stay in thin mode even when the libraries are
    installed — needed e.g. for TCPS with "Verify SSL" disabled, which only
    thin mode supports (thick mode's TLS trust comes from an Oracle wallet).
    """
    if os.getenv("ORACLE_THICK_MODE", "").strip().lower() in ("0", "false", "off", "thin", "no"):
        return False
    lib_dir = os.getenv("ORACLE_CLIENT_LIB_DIR", "").strip()
    if lib_dir:
        # Operator-supplied Instant Client (e.g. 11.2 for Oracle 10g servers,
        # which the bundled 19c client cannot talk to). start.sh also puts it
        # first on LD_LIBRARY_PATH so its dependent libraries resolve from the
        # same directory. If it fails to load, fall back to the default lookup
        # so a bad path degrades to today's behaviour instead of thin mode.
        try:
            oracledb.init_oracle_client(lib_dir=lib_dir)
            return True
        except Exception as e:
            logger.error(
                "ORACLE_CLIENT_LIB_DIR=%s could not be loaded (%s); "
                "falling back to the default Oracle Client libraries", lib_dir, e,
            )
    try:
        oracledb.init_oracle_client()
        return True
    except Exception:
        return False


def _env_seconds(name: str, default: float) -> Optional[float]:
    """Seconds from env; 0 or negative disables the limit."""
    try:
        value = float(os.getenv(name, default))
    except ValueError:
        value = default
    return value if value > 0 else None


# "Check connection" bounds. Without them a login the server never completes
# (an Oracle 10g server behind the 19c client) or a dictionary query that
# never returns left the UI on "Connecting…" forever with no error at all.
# Generous on purpose: a healthy login takes seconds, so these only ever fire
# on calls that were going to hang. Neither applies to indexing or queries.
CONNECT_TIMEOUT_S = _env_seconds("ORACLE_CONNECT_TIMEOUT_S", 60)
VALIDATION_TIMEOUT_S = _env_seconds("ORACLE_VALIDATION_TIMEOUT_S", 300)

CONNECT_TIMEOUT_MESSAGE = (
    "Oracle did not finish the login within {seconds}s. The server accepted the "
    "connection but never answered, which usually means the database is older "
    "than the Oracle client supports: the bundled client supports Oracle 11.2 and "
    "newer, and Oracle 10g needs an older Instant Client (set ORACLE_CLIENT_LIB_DIR "
    "on the backend). A firewall silently dropping traffic looks the same."
)
VALIDATION_TIMEOUT_MESSAGE = (
    "Connected to Oracle, but reading the table list did not finish within "
    "{seconds}s. On older or very large Oracle databases the data dictionary "
    "views (ALL_TAB_COLUMNS) can be very slow: set Schema to only the schemas "
    "you need, or ask your DBA to gather dictionary statistics "
    "(DBMS_STATS.GATHER_DICTIONARY_STATS)."
)
# Thin mode's refusal for pre-12.1 servers names no way out; add one.
_UNSUPPORTED_SERVER_HINT = (
    " Oracle servers older than 12.1 need thick mode with an Oracle Client that "
    "supports them (the bundled client covers 11.2+; for 10g set ORACLE_CLIENT_LIB_DIR)."
)


class OracledbClient(DataSourceClient):

    # Rendered into codegen prompts (<connection_clients>) so generated queries
    # express time windows with the engine's own relative date functions instead
    # of literal dates that go stale when saved code is re-executed.
    relative_date_hint = "Relative dates (Oracle): TRUNC(SYSDATE), TRUNC(SYSDATE) - 7, TRUNC(SYSDATE, 'MM') for month start; SYSDATE is the DB server's clock."

    # Read by ConnectionService's schema-access check (the second half of
    # "Check connection"); see VALIDATION_TIMEOUT_S.
    validation_timeout_s = VALIDATION_TIMEOUT_S
    validation_timeout_message = VALIDATION_TIMEOUT_MESSAGE

    def __init__(self, host, port, service_name, user, password, schema: Optional[str] = None,
                 use_tcps: bool = False, verify_ssl: bool = True):
        self.host = host
        self.port = port
        self.service_name = service_name
        self.user = user
        self.password = password
        self.use_tcps = use_tcps
        self.verify_ssl = verify_ssl
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

    def _connect_args(self) -> dict:
        """Extra DBAPI connect arguments for TCPS (TLS) connections.

        The SQLAlchemy dialect builds a plain-TCP connect descriptor from the
        URI, so for TCPS we override the dsn with an explicit descriptor —
        connect_args take precedence over dialect-generated parameters.
        Skipping certificate verification is only possible in thin mode
        (ssl_context is a thin-only parameter; thick mode trusts wallets).
        """
        if not self.use_tcps:
            return {}
        args = {
            "dsn": (
                f"(DESCRIPTION=(ADDRESS=(PROTOCOL=TCPS)(HOST={self.host})(PORT={self.port}))"
                f"(CONNECT_DATA=(SERVICE_NAME={self.service_name})))"
            )
        }
        if not self.verify_ssl:
            args["ssl_server_dn_match"] = False
            if oracledb.is_thin_mode():
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                args["ssl_context"] = ctx
        return args

    @contextmanager
    def connect(self) -> Generator[sqlalchemy.engine.base.Connection, None, None]:
        """Yield a connection to an Oracle database."""
        conn = None
        try:
            engine = get_engine(self.oracle_uri, connect_args=self._connect_args())
            conn = engine.connect()
            # Set current schema if provided (Oracle has no search_path; use first schema)
            if self._schemas:
                current_schema = self._schemas[0]
                try:
                    conn.execute(text(f'ALTER SESSION SET CURRENT_SCHEMA = {current_schema}'))
                except IndexingCancelled:
                    raise
                except Exception:
                    pass
        except IndexingCancelled:
            raise
        except Exception as e:
            if conn is not None:
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
        except IndexingCancelled:
            raise
        except Exception:
            return self._get_tables_basic()

    def _get_tables_enriched(self) -> List[Table]:
        """Get tables with column/table comments. May fail on some Oracle configurations."""
        discovery_phase('reading_columns')
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
            for row in discovery_items(result, 'columns', label=lambda row: '.'.join(str(v) for v in row[:3])):
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
            self._attach_foreign_keys(conn, tables)
            return list(tables.values())

    def _get_tables_basic(self) -> List[Table]:
        """Get tables without comments (original query - always works)."""
        discovery_phase('metadata_fallback')
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
                for row in discovery_items(result, 'columns', label=lambda row: '.'.join(str(v) for v in row[:3])):
                    owner, table_name, column_name, data_type = row
                    key = (owner, table_name)
                    fqn = f"{owner}.{table_name}"
                    if key not in tables:
                        tables[key] = Table(
                            name=fqn, columns=[], pks=[], fks=[], metadata_json={"schema": owner}
                        )
                    tables[key].columns.append(TableColumn(name=column_name, dtype=data_type))
                self._attach_foreign_keys(conn, tables)
            return list(tables.values())
        except IndexingCancelled:
            raise
        except Exception as e:
            print(f"Error retrieving tables: {e}")
            return []

    def get_schema(self, table_id: str) -> Table:
        """This method is now obsolete. Please use get_tables() instead."""
        raise NotImplementedError(
            "get_schema() is obsolete. Use get_tables() instead.")

    def _attach_foreign_keys(self, conn, tables) -> None:
        """Populate `fks` on the tables just collected.

        `name_fn` mirrors the `f"{owner}.{table_name}"` this client uses for
        `Table.name` — the two must agree or downstream resolution, which is an
        exact string match, drops every edge without complaining.
        """
        attach_foreign_keys(
            conn,
            tables,
            self._schemas or None,
            lambda schema, table: f"{schema}.{table}",
        )

    @discovery_progress
    def get_schemas(self, progress_callback=None):
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
            message = str(e)
            if "DPY-3010" in message:
                message += _UNSUPPORTED_SERVER_HINT
            return {
                "success": False,
                "message": message
            }

    async def atest_connection(self):
        """Base probe, bounded by CONNECT_TIMEOUT_S (see its comment)."""
        if CONNECT_TIMEOUT_S is None:
            return await super().atest_connection()
        from app.data_sources.engine_pool import ephemeral

        def _probe():
            with ephemeral():
                return self.test_connection()

        try:
            return await run_bounded(self, _probe, CONNECT_TIMEOUT_S)
        except SourceCallTimeout:
            return {
                "success": False,
                "message": CONNECT_TIMEOUT_MESSAGE.format(seconds=int(CONNECT_TIMEOUT_S)),
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

        Character set mismatch (ORA-12704):
        Oracle raises "ORA-12704: character set mismatch" when a single expression
        combines text values of different character sets — a national-charset column
        (NVARCHAR2 / NCHAR / NCLOB) mixed with a database-charset value (VARCHAR2 /
        CHAR / CLOB / a plain 'literal'). Watch the column types in the schema: a
        column typed NVARCHAR2 or NCHAR is national charset.
        This happens most often in:
          - UNION / UNION ALL where one branch's text column is NVARCHAR2 and the
            matching column in another branch is VARCHAR2 (or a plain string literal).
          - CASE / DECODE / COALESCE / NVL whose branches mix NVARCHAR2 and VARCHAR2.
          - String concatenation (||) or comparison across the two charsets.
        To avoid it, normalize every text branch to ONE charset. The simplest, most
        reliable fix is to wrap each text column and literal in TO_CHAR(...) so all
        branches share the database charset:
          - TO_CHAR(nvarchar2_col)  -- national -> database charset
          - Use TO_CHAR('literal') or just a plain 'literal' consistently.
        Example (fixes a UNION across a VARCHAR2 and an NVARCHAR2 column):
          SELECT TO_CHAR(status) AS status FROM DWH.WF_PROC_INSTS
          UNION ALL
          SELECT TO_CHAR(state)  AS status FROM DWH.WF_MANUAL_WORKITEMS
        Alternatively bring everything to the national charset with TO_NCHAR(...),
        but pick one direction and apply it to every branch — do not leave a raw
        NVARCHAR2 column next to a VARCHAR2 one.
        """
        description = f"Oracle database service '{self.service_name}' at {self.host}:{self.port}\n\n"
        description += system_prompt
        return description
