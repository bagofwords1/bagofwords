import base64
import logging
import re
import time
from collections.abc import Generator, Iterable
from contextlib import contextmanager

import pandas as pd
import sqlalchemy
from sqlalchemy.engine import URL

from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import (
    IndexingCancelled,
    discovery_items,
    discovery_phase,
    discovery_progress,
)
from app.data_sources.engine_pool import get_engine
from app.data_sources.query_cancellation import track

try:
    import teradatasql
except ImportError:
    teradatasql = None

if teradatasql is not None:
    # Registers the `teradatasql_bow://` SQLAlchemy dialect used for pooling.
    from app.data_sources import teradata_dialect

logger = logging.getLogger(__name__)

# View column types: HELP COLUMN runs once per view, so it gets a time budget.
# Views not reached keep an empty type; the rest of discovery is unaffected.
VIEW_TYPE_BUDGET_SECONDS = 120

# Object kinds the agent can SELECT from (DBC TableKind):
# T table, V view, O table without a primary index, Q queue table.
_TABLE_KINDS = ("T", "V", "O", "Q")
_KIND_NAMES = {"T": "table", "V": "view", "O": "no_primary_index_table", "Q": "queue_table"}

# Teradata stores column types as a 2-char code (DBC.ColumnsV.ColumnType,
# HELP COLUMN "Type"). Map them to readable names for the agent prompt; an
# unknown code falls back to itself.
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
    "PD": "PERIOD(DATE)",
    "PT": "PERIOD(TIME)",
    "PZ": "PERIOD(TIME WITH TIME ZONE)",
    "PS": "PERIOD(TIMESTAMP)",
    "PM": "PERIOD(TIMESTAMP WITH TIME ZONE)",
    "A1": "ARRAY",
    "AN": "ARRAY",
    "UT": "UDT",
    "++": "TD_ANYTYPE",
}
# DBC CharType: 1 LATIN, 2 UNICODE, 3 KANJISJIS, 4 GRAPHIC, 5 KANJI1.
# ColumnLength is in bytes; UNICODE and GRAPHIC store 2 bytes per character.
_CHAR_SETS = {1: "LATIN", 2: "UNICODE", 3: "KANJISJIS", 4: "GRAPHIC", 5: "KANJI1"}
_DOUBLE_BYTE_CHAR_TYPES = {2, 4}

# Driver parameters an admin may add under "Additional connection parameters".
# An allowlist: everything that decides identity, encryption, certificate
# checks or result shape stays owned by the form's own fields.
ALLOWED_EXTRA_PARAMS = {
    "account", "cop", "coplast", "charset", "client_charset", "tmode",
    "https_port", "connect_timeout", "logon_timeout", "request_timeout",
    "reconnect_count", "reconnect_interval", "max_message_body", "sip_support",
}

_SSL_MODES = {"DISABLE", "ALLOW", "PREFER", "REQUIRE", "VERIFY-CA", "VERIFY-FULL"}

# "[Version 20.0.0.74] [Session 1234] " — driver build and session number; noise
# for the user and the agent.
_ERROR_PREFIX = re.compile(r"^(?:\[(?:Version|Session) [^\]]*\]\s*)+")


def short_error(exc: BaseException) -> str:
    """One readable line from a teradatasql error.

    Driver messages run to dozens of lines: the error, then a Go stack trace
    ("at gosqldriver/teradatasql.formatError ErrorUtil.go:84", ...). Teradata's
    own samples print only the first line. The code (e.g. "[Error 3807]") is on
    it, so callers matching on codes keep working.
    """
    orig = getattr(exc, "orig", None) or exc
    text = str(orig).strip()
    first = text.splitlines()[0].strip() if text else type(orig).__name__
    return _ERROR_PREFIX.sub("", first) or first


def _shorten(exc: Exception) -> Exception:
    """The driver's own exception type with a one-line message."""
    orig = getattr(exc, "orig", None) or exc
    msg = short_error(orig)
    try:
        new = type(orig)(msg)
    except Exception:
        new = RuntimeError(msg)
    return new


class TeradataClient(DataSourceClient):

    # Teradata: '' only (sqlglot teradata).
    sql_literal_style = "standard"

    relative_date_hint = (
        "Relative dates (Teradata): CURRENT_DATE, CURRENT_DATE - 7, "
        "ADD_MONTHS(CURRENT_DATE, -1), TRUNC(CURRENT_DATE, 'MM') for month start; "
        "CURRENT_DATE uses the session time zone only when the server's "
        "TimeDateWZControl setting is on, otherwise the server's clock."
    )

    def __init__(self, host, database, user, password, port: int = 1025,
                 logmech: str | None = "TD2", sslmode: str | None = "PREFER",
                 ssl_ca: str | None = None, encryptdata: bool = True,
                 additional_params: dict | None = None):
        if teradatasql is None:
            raise ImportError(
                "teradatasql is required for Teradata connections. "
                "Please install it with: pip install teradatasql"
            )
        self.host = (host or "").strip()
        self.port = int(port or 1025)
        self.database = database
        self.user = user
        self.password = password
        # Empty/None logmech means use the driver default (TD2).
        self.logmech = (logmech or "").strip() or None
        mode = (sslmode or "").strip().upper() or "PREFER"
        if mode not in _SSL_MODES:
            raise ValueError(f"Unsupported SSL mode '{sslmode}'. Use one of: {', '.join(sorted(_SSL_MODES))}.")
        self.sslmode = mode
        self.ssl_ca = (ssl_ca or "").strip() or None
        self.encryptdata = bool(encryptdata)

        self.extra_params: dict[str, str] = {}
        self.ignored_params: list[str] = []
        for key, value in (additional_params or {}).items():
            k = str(key or "").strip().lower()
            if not k:
                continue
            if k not in ALLOWED_EXTRA_PARAMS:
                self.ignored_params.append(str(key))
                logger.warning("Teradata: ignoring unsupported additional parameter %r", key)
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            self.extra_params[k] = str(value)

        # `database` may be a single database or a comma-separated list. In
        # Teradata a "database" is the namespace (≈ schema in other engines).
        self._databases: list[str] = []
        if isinstance(self.database, str) and self.database.strip():
            seen = set()
            for part in self.database.split(","):
                part = part.strip()
                if part and part.upper() not in seen:
                    seen.add(part.upper())
                    self._databases.append(part)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    @property
    def teradata_uri(self) -> str:
        """Pool identity: host, port, user, password and default database.

        Also what `connection_service._invalidate_engine_pool` disposes when
        the connection is edited or deleted.
        """
        return URL.create(
            teradata_dialect.DRIVERNAME,
            username=self.user or None,
            password=self.password or None,
            host=self.host,
            port=self.port,
            database=self._databases[0] if self._databases else None,
        ).render_as_string(hide_password=False)

    def _connect_args(self) -> dict[str, str]:
        """Driver parameters beyond the URI. Every value is a string, as the
        driver requires."""
        args: dict[str, str] = dict(self.extra_params)
        if self.logmech:
            args["logmech"] = self.logmech
        args["sslmode"] = self.sslmode
        if self.ssl_ca:
            # sslbase64: base64url of the PEM, so no CA file has to exist on disk.
            args["sslbase64"] = base64.urlsafe_b64encode(self.ssl_ca.encode()).decode().rstrip("=")
        args["encryptdata"] = "true" if self.encryptdata else "false"
        return args

    @contextmanager
    def connect(self) -> Generator[sqlalchemy.engine.Connection]:
        """Yield a pooled SQLAlchemy connection to Teradata.

        Pooled like the other SQL clients (see engine_pool): a Teradata logon
        is slow and logged, so each query reuses a warm session. The driver's
        autocommit is on, so no session is returned to the pool mid-transaction.
        """
        conn = None
        try:
            engine = get_engine(self.teradata_uri, connect_args=self._connect_args())
            conn = engine.connect()
        except IndexingCancelled:
            raise
        except Exception as e:
            if conn is not None:
                conn.close()
            logger.debug("Teradata connect failed: %s", e)
            raise RuntimeError(short_error(e)) from None
        # The yield is outside the try above so exceptions raised by the
        # caller's `with` body keep their type (see mssql_client.connect).
        try:
            # Registered so a timed-out query can be cancelled on the server
            # (query_cancellation calls the driver's cancel()).
            with track(self, conn):
                yield conn
        finally:
            conn.close()

    def _run(self, conn, sql: str, params: Iterable = ()) -> list:
        return conn.exec_driver_sql(sql, tuple(params)).fetchall()

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute SQL statement and return the result as a DataFrame."""
        with self.connect() as conn:
            try:
                # A str goes to exec_driver_sql: the text reaches Teradata as
                # written, with no SQLAlchemy bind-parameter parsing.
                return pd.read_sql(sql, conn)
            except IndexingCancelled:
                raise
            except Exception as e:
                logger.debug("Teradata query failed: %s", e)
                raise _shorten(e) from None

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _db_filter(self, column: str = "DatabaseName") -> tuple[str, list]:
        if not self._databases:
            return "", []
        placeholders = ", ".join(["?"] * len(self._databases))
        return f" AND {column} IN ({placeholders})", list(self._databases)

    def _first_working(self, conn, attempts: list[tuple[str, list]], what: str,
                       expected_to_fail: bool = False) -> list | None:
        """Run the first statement that succeeds; None when all fail.

        Lets discovery degrade instead of failing: the access-filtered X views
        first, the unfiltered views next; the full column list first, the
        basic one next (older releases and restricted DBC grants).
        """
        last = None
        for sql, params in attempts:
            try:
                return self._run(conn, sql, params)
            except IndexingCancelled:
                raise
            except Exception as e:
                last = e
                logger.info("Teradata %s query failed, trying fallback: %s", what, short_error(e))
        if last is not None:
            log = logger.info if expected_to_fail else logger.warning
            log("Teradata %s unavailable: %s", what, short_error(last))
        return None

    def count_tables(self) -> int:
        """Cheap object count for the connection test (one row per object), so
        the test does not read every column of every table."""
        db_sql, params = self._db_filter()
        kinds = ", ".join(f"'{k}'" for k in _TABLE_KINDS)
        with self.connect() as conn:
            rows = self._first_working(conn, [
                (f"SELECT COUNT(*) FROM DBC.TablesVX WHERE TableKind IN ({kinds}){db_sql}", params),
                (f"SELECT COUNT(*) FROM DBC.TablesV WHERE TableKind IN ({kinds}){db_sql}", params),
            ], "table count")
        if rows is None:
            raise RuntimeError("Could not read DBC.TablesV")
        return int(rows[0][0] or 0)

    def get_tables(self) -> list[Table]:
        """Tables and views with typed columns, comments and primary indexes.

        Uses the X views (DBC.TablesVX / ColumnsVX / IndicesVX), which list only
        objects this user can access, so the agent is not shown tables it would
        get error 3523 on. View column types come from DBC.ColumnsQVX when the
        server has QVCI enabled, otherwise from HELP COLUMN per view.
        """
        kinds = ", ".join(f"'{k}'" for k in _TABLE_KINDS)
        db_sql, params = self._db_filter()
        with self.connect() as conn:
            discovery_phase("tables")
            objects = self._first_working(conn, [
                (f"SELECT DatabaseName, TableName, TableKind, CommentString FROM DBC.TablesVX "
                 f"WHERE TableKind IN ({kinds}){db_sql}", params),
                (f"SELECT DatabaseName, TableName, TableKind, CommentString FROM DBC.TablesV "
                 f"WHERE TableKind IN ({kinds}){db_sql}", params),
            ], "table list")
            if objects is None:
                raise RuntimeError("Could not read the table list from DBC.TablesV")

            tables: dict[tuple[str, str], Table] = {}
            kinds_by_key: dict[tuple[str, str], str] = {}
            for database_name, table_name, kind, comment in objects:
                key = (_s(database_name), _s(table_name))
                kind = _s(kind)
                kinds_by_key[key] = kind
                tables[key] = Table(
                    name=f"{key[0]}.{key[1]}",
                    description=_s(comment) or None,
                    columns=[],
                    pks=None,
                    fks=None,
                    metadata_json={
                        "schema": key[0],
                        "teradata": {"kind": _KIND_NAMES.get(kind, kind)},
                    },
                )

            full_cols = ("DatabaseName, TableName, ColumnName, ColumnType, ColumnLength, "
                         "DecimalTotalDigits, DecimalFractionalDigits, CharType, ColumnUDTName, CommentString")
            basic_cols = ("DatabaseName, TableName, ColumnName, ColumnType, NULL, NULL, NULL, NULL, NULL, "
                          "CommentString")
            order = " ORDER BY DatabaseName, TableName, ColumnId"
            col_db_sql, col_params = self._db_filter()
            rows = self._first_working(conn, [
                (f"SELECT {full_cols} FROM DBC.ColumnsVX WHERE 1=1{col_db_sql}{order}", col_params),
                (f"SELECT {basic_cols} FROM DBC.ColumnsVX WHERE 1=1{col_db_sql}{order}", col_params),
                (f"SELECT {full_cols} FROM DBC.ColumnsV WHERE 1=1{col_db_sql}{order}", col_params),
                (f"SELECT {basic_cols} FROM DBC.ColumnsV WHERE 1=1{col_db_sql}{order}", col_params),
            ], "column list")
            if rows is None:
                raise RuntimeError("Could not read columns from DBC.ColumnsV")

            for row in discovery_items(rows, "columns", label=lambda r: f"{_s(r[0])}.{_s(r[1])}.{_s(r[2])}"):
                key = (_s(row[0]), _s(row[1]))
                table = tables.get(key)
                if table is None:
                    continue  # join index, macro, ... — not queryable as a table
                table.columns.append(TableColumn(
                    name=_s(row[2]),
                    dtype=format_dtype(row[3], row[4], row[5], row[6], row[7], row[8]),
                    description=_s(row[9]) or None,
                ))

            views = [k for k, kind in kinds_by_key.items()
                     if kind == "V" and any(not c.dtype for c in tables[k].columns)]
            if views:
                self._fill_view_types(conn, tables, views)

            self._attach_primary_indexes(conn, tables)

        return list(tables.values())

    def _fill_view_types(self, conn, tables, views) -> None:
        """Type view columns, which DBC.ColumnsV reports with a NULL type."""
        db_sql, params = self._db_filter()
        qv = self._first_working(conn, [
            (f"SELECT DatabaseName, TableName, ColumnName, ColumnType, ColumnLength, "
             f"DecimalTotalDigits, DecimalFractionalDigits, CharType, ColumnUDTName "
             f"FROM DBC.ColumnsQVX WHERE 1=1{db_sql}", params),
        ], "view column types (ColumnsQVX; needs QVCI enabled)", expected_to_fail=True)
        pending = set(views)
        if qv:
            wanted = {k: {c.name.upper(): c for c in tables[k].columns} for k in views}
            for row in qv:
                key = (_s(row[0]), _s(row[1]))
                col = wanted.get(key, {}).get(_s(row[2]).upper())
                if col is not None and not col.dtype:
                    col.dtype = format_dtype(row[3], row[4], row[5], row[6], row[7], row[8])
            pending = {k for k in views if any(not c.dtype for c in tables[k].columns)}
        if not pending:
            return

        deadline = time.monotonic() + VIEW_TYPE_BUDGET_SECONDS
        items = discovery_items(sorted(pending), "view_columns", label=lambda k: f"{k[0]}.{k[1]}")
        for done, key in enumerate(items):
            if time.monotonic() > deadline:
                logger.warning(
                    "Teradata: view column types stopped after %ss; %d of %d views left untyped",
                    VIEW_TYPE_BUDGET_SECONDS, len(pending) - done, len(pending),
                )
                break
            try:
                result = conn.exec_driver_sql(f"HELP COLUMN {_quote(key[0])}.{_quote(key[1])}.*")
                cols = {c.name.upper(): c for c in tables[key].columns}
                for r in result.mappings():
                    name = _s(r.get("Column Dictionary Name") or r.get("Column Name"))
                    col = cols.get(name.upper())
                    if col is not None and not col.dtype:
                        col.dtype = format_dtype(
                            r.get("Type"), r.get("Max Length"), r.get("Decimal Total Digits"),
                            r.get("Decimal Fractional Digits"), r.get("Char Type"),
                            r.get("UDT Dictionary Name") or r.get("UDT Name"),
                        )
            except IndexingCancelled:
                raise
            except Exception as e:
                # A view over a dropped table, or one this user cannot SELECT.
                logger.info("Teradata HELP COLUMN failed for %s.%s: %s", key[0], key[1], short_error(e))

    def _attach_primary_indexes(self, conn, tables) -> None:
        """Primary index columns per table: Teradata's distribution key and the
        cheapest join path. A declared primary key (IndexType K) or a unique
        primary index also becomes the table's `pks`."""
        db_sql, params = self._db_filter()
        select = ("SELECT DatabaseName, TableName, ColumnName, IndexType, UniqueFlag, ColumnPosition "
                  "FROM DBC.{view} WHERE IndexType IN ('P', 'Q', 'K', 'A'){db} "
                  "ORDER BY DatabaseName, TableName, IndexType, ColumnPosition")
        rows = self._first_working(conn, [
            (select.format(view="IndicesVX", db=db_sql), params),
            (select.format(view="IndicesV", db=db_sql), params),
        ], "primary indexes")
        if not rows:
            return
        found: dict[tuple[str, str], dict[str, list]] = {}
        unique: dict[tuple[str, str], bool] = {}
        for database_name, table_name, column_name, index_type, unique_flag, _pos in rows:
            key = (_s(database_name), _s(table_name))
            if key not in tables:
                continue
            itype = "K" if _s(index_type) == "K" else "PI"
            found.setdefault(key, {}).setdefault(itype, []).append(_s(column_name))
            if itype == "PI":
                unique[key] = _s(unique_flag).upper() == "Y"
        for key, idx in found.items():
            table = tables[key]
            by_name = {c.name.upper(): c for c in table.columns}
            meta = table.metadata_json.setdefault("teradata", {})
            if "PI" in idx:
                meta["primary_index"] = ", ".join(idx["PI"])
                meta["primary_index_unique"] = unique.get(key, False)
            key_cols = idx.get("K") or (idx.get("PI") if unique.get(key) else None)
            if key_cols:
                table.pks = [by_name.get(n.upper()) or TableColumn(name=n, dtype=None) for n in key_cols]

    def get_schema(self, table_id: str) -> Table:
        """This method is now obsolete. Please use get_tables() instead."""
        raise NotImplementedError(
            "get_schema() is obsolete. Use get_tables() instead.")

    @discovery_progress
    def get_schemas(self, progress_callback=None):
        """Get schemas for all tables in the specified database(s)."""
        return self.get_tables()

    def prompt_schema(self):
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        """Test connection to Teradata and return status information."""
        if self.ignored_params:
            # Fail the test rather than drop them quietly: the admin typed them
            # expecting an effect. A saved connection still runs without them.
            return {
                "success": False,
                "message": (
                    f"Unsupported additional parameters: {', '.join(sorted(self.ignored_params))}. "
                    f"Allowed: {', '.join(sorted(ALLOWED_EXTRA_PARAMS))}."
                ),
            }
        try:
            with self.connect() as conn:
                self._run(conn, "SELECT 1")
        except Exception as e:
            return {"success": False, "message": short_error(e)}
        return {"success": True, "message": "Successfully connected to Teradata"}

    @property
    def description(self):
        # Only the connection's own override is visible here; the org default
        # is resolved by the code-execution wrapper. Don't state a number we
        # cannot know.
        timeout = getattr(self, "_bow_connection_query_timeout", None)
        timeout_note = (f"Queries time out after {timeout}s" if timeout
                        else "Queries have a time limit")
        system_prompt = f"""
        This is a Teradata Vantage database (Teradata SQL dialect).
        You can call the execute_query method to run SQL queries.

        Table references: tables are provided as database.table (Teradata
        "databases" are the namespace, equivalent to schemas elsewhere). Use
        them exactly as given, without quotes — e.g. SELECT * FROM exdb.ex_orders.

        Read-only: this connection only runs SELECT (plus HELP / SHOW / EXPLAIN).
        INSERT, UPDATE, DELETE, MERGE, CREATE, DROP and the like are refused before
        they reach Teradata. Never offer to change data; say it is read-only.

        Sample queries (exdb.ex_* are placeholder tables — use only the tables and
        columns in the schema):

        ```python
        # Preview rows (use TOP, not LIMIT)
        df = client.execute_query("SELECT TOP 10 * FROM exdb.ex_orders ORDER BY order_date DESC")
        ```
        ```python
        # Aggregate with a date filter; CAST before SUM so large totals do not overflow
        df = client.execute_query(
            "SELECT order_status, COUNT(*) AS cnt, SUM(CAST(amount AS DECIMAL(38,2))) AS total "
            "FROM exdb.ex_orders "
            "WHERE order_date BETWEEN DATE '2024-01-01' AND DATE '2024-12-31' "
            "GROUP BY order_status ORDER BY total DESC"
        )
        ```
        ```python
        # Latest row per group using QUALIFY (Teradata-specific; no TOP in the same SELECT)
        df = client.execute_query(
            "SELECT customer_id, order_id, order_date FROM exdb.ex_orders "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_date DESC) = 1"
        )
        ```
        ```python
        # Join across databases and bucket by month
        df = client.execute_query(
            "SELECT TRUNC(o.order_date, 'MON') AS month_start, c.region, SUM(o.amount) AS revenue "
            "FROM exdb.ex_orders o "
            "JOIN exdb.ex_customers c ON o.customer_id = c.customer_id "
            "GROUP BY 1, 2 ORDER BY 1, 2"
        )
        ```
        ```python
        # Top-N per group with a tie-safe rank
        df = client.execute_query(
            "SELECT product_id, region, revenue FROM exdb.ex_product_sales "
            "QUALIFY RANK() OVER (PARTITION BY region ORDER BY revenue DESC) <= 5"
        )
        ```

        IMPORTANT - Teradata dialect differences:

        Row limits: use TOP n right after SELECT, e.g. SELECT TOP 100 * FROM exdb.ex_orders.
            LIMIT and OFFSET are NOT supported.
            TOP cannot be combined with QUALIFY, SAMPLE or DISTINCT in the same SELECT —
            use QUALIFY ROW_NUMBER() OVER (ORDER BY ...) <= n instead, or wrap the
            query in a derived table and put TOP on the outer SELECT.
        Date literals: DATE '2024-01-31', TIMESTAMP '2024-01-31 10:00:00'.
        Current date/time: CURRENT_DATE, CURRENT_TIMESTAMP, CURRENT_TIME.
        Date arithmetic: CURRENT_DATE - 7, date + INTERVAL '7' DAY (singular DAY/MONTH),
            ADD_MONTHS(date, n), EXTRACT(YEAR FROM date), TRUNC(date, 'MON') for month
            bucketing. Use CAST(x AS DATE) for casts.
        Window filtering: QUALIFY filters window functions directly, e.g.
            SELECT ... QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1.
        Sampling: SAMPLE n (rows) or SAMPLE 0.1 (fraction).
        String aggregation: use XMLAGG(...) patterns; there is no GROUP_CONCAT or STRING_AGG.
        Concatenation: || (NULL propagates — wrap with COALESCE if needed).
        NULL handling: COALESCE(a, b) and NULLIF(a, b) are supported.
        Not-equal: use <>.
        Dirty reads: you may prefix a query with LOCKING ROW FOR ACCESS so it does not wait
            behind loads that hold write locks.
        Column metadata: HELP COLUMN database.table.* lists a table's or view's columns.

        DO NOT use: LIMIT, OFFSET, ILIKE, !=, GENERATE_SERIES(), STRING_AGG().

        Common errors and fixes:
        - [Error 2616] Numeric overflow: SUM/COUNT over INTEGER columns can exceed the
          INTEGER range — CAST(col AS BIGINT) or CAST(col AS DECIMAL(38,2)) inside SUM.
        - [Error 6706] Untranslatable character: mixing LATIN and UNICODE text (UNION,
          CASE, ||, comparisons). Columns typed "CHARACTER SET UNICODE" are UNICODE; wrap
          the others with TRANSLATE(col USING LATIN_TO_UNICODE) or CAST(col AS VARCHAR(n)
          CHARACTER SET UNICODE).
        - [Error 3504] Selected non-aggregate values must be part of the associated group:
          every non-aggregated column must appear in GROUP BY.
        - [Error 3706] Syntax error: usually LIMIT, ILIKE, or TOP combined with QUALIFY.
        - [Error 3523] The user does not have SELECT access: pick another table; do not retry.

        Performance notes:
        - Avoid wrapping filtered/joined columns in functions — breaks index use.
        - Join on primary index columns when you can (listed as primary_index on the table).
        - Avoid leading-wildcard LIKE '%x%' on large tables.
        - Prefer exact match or IN (...) over text search on large columns.
        - {timeout_note} and are then cancelled on the server. Narrow date ranges
          first; don't rely on retries.
        """
        description = f"Teradata Vantage database '{self.database}' at {self.host}:{self.port}\n\n"
        description += system_prompt
        return description


def _s(value) -> str:
    """DBC columns are CHAR and come back space-padded."""
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value))


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def format_dtype(code, length=None, total_digits=None, fractional_digits=None,
                 char_type=None, udt_name=None) -> str:
    """Readable column type from DBC/HELP COLUMN fields, e.g.
    VARCHAR(100) CHARACTER SET UNICODE, DECIMAL(18,2), SYSUDTLIB.ST_GEOMETRY."""
    key = _s(code)
    if not key:
        return ""
    base = _TD_TYPE_CODES.get(key, key)
    udt = _s(udt_name)
    if key in ("UT", "A1", "AN") and udt:
        return f"ARRAY {udt}" if key in ("A1", "AN") else udt
    if key in ("CF", "CV"):
        n = _int(length)
        ct = _int(char_type)
        if n and n > 0:
            if ct in _DOUBLE_BYTE_CHAR_TYPES:
                n //= 2
            base = f"{base}({n})"
        if ct and ct != 1 and ct in _CHAR_SETS:
            base += f" CHARACTER SET {_CHAR_SETS[ct]}"
        return base
    if key in ("D", "N"):
        p, s = _int(total_digits), _int(fractional_digits)
        # NUMBER without a declared precision reports a sentinel outside 1..38.
        if p and 1 <= p <= 38:
            return f"{base}({p},{s if s and s >= 0 else 0})"
    return base
