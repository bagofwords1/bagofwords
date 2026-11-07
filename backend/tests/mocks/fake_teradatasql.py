"""A stand-in for the `teradatasql` driver, backed by DuckDB.

No Teradata server can run in CI or a sandbox (Vantage Express is a licensed VM
image), so tests and the sandbox feedback loop swap this module in for
`teradatasql` — the driver boundary, per backend/tests/AGENTS.md. It mimics the
behaviours of the real driver and database that the client depends on, each
taken from the driver README, Teradata's Data Dictionary docs, or observed
against the real driver (20.0.0.74):

  * every connection parameter is checked against the driver's documented
    names; an unknown one fails like the real driver ("[Error 169] Unable to
    parse JSON connection parameters")
  * error text has the real shape: "[Version ...] [Session n] [Teradata
    Database] [Error nnnn] message", then a Go stack trace
  * DBC.TablesV / ColumnsV / IndicesV list every object; the X views only the
    ones the user may access. ColumnsV has NULL ColumnType for view columns;
    DBC.ColumnsQV(X) fills them only when QVCI is enabled
  * HELP COLUMN returns the real column titles ("Column Dictionary Name",
    "Type", "Max Length", ...)
  * no LIMIT / ILIKE; TOP cannot be combined with QUALIFY (Error 3706)
  * Connection.cancel() from another thread aborts the running request
  * selecting from a table without access fails with Error 3523

It is not a Teradata emulator: data queries are transpiled to DuckDB.
Counters live in STATS (and in the JSON file named by FAKE_TERADATA_STATS) so a
test or a person can see logons, requests, cancels and writes that arrived.
"""

import contextlib
import json
import os
import re
import threading
import time

import duckdb
import sqlglot

apilevel = "2.0"
threadsafety = 2
paramstyle = "qmark"


class Warning(Exception):  # noqa: A001 - DB-API name
    pass


class Error(Exception):
    pass


class InterfaceError(Error):
    pass


class DatabaseError(Error):
    pass


class OperationalError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


class DataError(DatabaseError):
    pass


class IntegrityError(DatabaseError):
    pass


class InternalError(DatabaseError):
    pass


class NotSupportedError(DatabaseError):
    pass


VERSION = "20.0.0.74"

# Connection parameter names from the driver README's parameter table.
KNOWN_PARAMS = {
    "account", "browser", "browser_tab_timeout", "browser_timeout", "charset", "client_charset",
    "code_append_file", "column_name", "concurrent_interval", "concurrent_limit",
    "connect_failure_ttl", "connect_function", "connect_timeout", "cop", "coplast", "database",
    "dbs_port", "encryptdata", "error_query_count", "error_query_interval", "fake_result_sets",
    "field_quote", "field_sep", "gateway_deadline", "govern", "host", "http_proxy",
    "http_proxy_password", "http_proxy_user", "https_port", "https_proxy", "https_proxy_password",
    "https_proxy_user", "https_retry", "jws_algorithm", "jws_audience", "jws_cert",
    "jws_private_key", "lob_support", "local_catalog", "log", "logdata", "logmech",
    "logon_sequence_number", "logon_timeout", "manage_error_tables", "max_message_body",
    "no_system_proxy", "oauth_level", "oauth_scopes", "oidc_cache_size", "oidc_claim",
    "oidc_clientid", "oidc_metadata", "oidc_metadata_cache", "oidc_prompt", "oidc_redirect_port",
    "oidc_refresh", "oidc_refresh_percent", "oidc_scope", "oidc_sslmode", "oidc_token", "password",
    "proxy_bypass_hosts", "reconnect_count", "reconnect_interval", "redrive", "request_timeout",
    "runstartup", "sessions", "sip_support", "sp_spl", "sslbase64", "sslca", "sslcapath",
    "sslcipher", "sslcrc", "sslcrl", "sslmode", "sslnamedgroups", "sslocsp", "sslprotocol",
    "teradata_values", "tmode", "user", "use_system_proxy",
}

USERS = {"bow_reader": "Reader#2026"}
UNREACHABLE_HOSTS = {"unreachable.invalid"}

# Objects the fake user may SELECT from; everything else is listed only by the
# non-X DBC views and fails with Error 3523.
_ACCESSIBLE_DATABASES = {"sales", "finance"}

# (database, object, kind, comment, columns, primary index)
# column: (name, type code, length, total digits, fractional digits, char type, udt, comment)
_CATALOG = [
    ("sales", "orders", "T", "One row per customer order", [
        ("order_id", "I", 4, None, None, None, None, "Order number"),
        ("customer_id", "I", 4, None, None, None, None, None),
        ("order_date", "DA", 4, None, None, None, None, None),
        ("amount", "D", 8, 12, 2, None, None, "Order total in USD"),
        ("status", "CV", 20, None, None, 1, None, None),
        ("note", "CV", 400, None, None, 2, None, "Free text, UNICODE"),
    ], ("P", "Y", ["order_id"])),
    ("sales", "customers", "T", "Customer master", [
        ("customer_id", "I", 4, None, None, None, None, None),
        ("name", "CV", 200, None, None, 2, None, None),
        ("region", "CF", 4, None, None, 1, None, None),
        ("signup_ts", "TS", 19, None, None, None, None, None),
    ], ("K", "Y", ["customer_id"])),
    ("sales", "order_events", "O", "Order lifecycle events (no primary index)", [
        ("event_id", "I8", 8, None, None, None, None, None),
        ("order_id", "I", 4, None, None, None, None, None),
        ("valid_period", "PD", 8, None, None, None, None, None),
    ], None),
    ("sales", "slow_scan", "T", "A table whose scans never finish (cancel tests)", [
        ("id", "I", 4, None, None, None, None, None),
    ], ("P", "N", ["id"])),
    ("sales", "v_monthly_revenue", "V", "Revenue by month and region", [
        ("month_start", "DA", 4, None, None, None, None, None),
        ("region", "CF", 4, None, None, 1, None, None),
        ("revenue", "D", 16, 18, 2, None, None, None),
    ], None),
    ("finance", "product_sales", "T", None, [
        ("product_id", "I", 4, None, None, None, None, None),
        ("region", "CF", 4, None, None, 1, None, None),
        ("revenue", "D", 16, 18, 2, None, None, None),
        ("units", "I", 4, None, None, None, None, None),
    ], ("P", "N", ["product_id", "region"])),
    ("finance", "v_broken", "V", "A view over a dropped table", [
        ("x", "I", 4, None, None, None, None, None),
    ], None),
    ("hr", "salaries", "T", "Restricted", [
        ("employee_id", "I", 4, None, None, None, None, None),
        ("salary", "D", 8, 12, 2, None, None, None),
    ], ("P", "Y", ["employee_id"])),
]
# A join index lives in DBC.TablesV but cannot be selected from like a table.
_JOIN_INDEXES = [("sales", "ji_orders_by_date")]

_SQL_TYPES = {"I": "INTEGER", "I8": "BIGINT", "DA": "DATE", "TS": "TIMESTAMP", "CF": "VARCHAR",
              "CV": "VARCHAR", "PD": "VARCHAR"}

STATS = {"logons": 0, "logoffs": 0, "requests": 0, "cancels": 0, "active": 0,
         "writes_seen": [], "statements": [], "last_logon": None}
_stats_lock = threading.Lock()
_db_lock = threading.Lock()
_db = None
_sessions = iter(range(1000, 10_000_000))


def _bump(**changes):
    with _stats_lock:
        for k, v in changes.items():
            if k in ("writes_seen", "statements"):
                STATS[k].append(v)
                del STATS[k][:-50]
            elif k == "last_logon":
                STATS[k] = v
            else:
                STATS[k] += v
        path = os.environ.get("FAKE_TERADATA_STATS")
        if path:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(STATS, f, default=str, indent=2)
            os.replace(tmp, path)


def reset():
    """Test helper: fresh counters and data."""
    global _db
    with _stats_lock:
        STATS.update({"logons": 0, "logoffs": 0, "requests": 0, "cancels": 0, "active": 0,
                      "writes_seen": [], "statements": [], "last_logon": None})
    with _db_lock:
        _db = None


def qvci_enabled() -> bool:
    return os.environ.get("FAKE_TERADATA_QVCI", "0") == "1"


def _error(code, message, session=0, source="Teradata Database"):
    trace = "\n".join(
        f" at gosqldriver/teradatasql.{fn} {file}"
        for fn, file in [
            ("formatError", "ErrorUtil.go:84"), ("(*teradataConnection).processErrorParcel", "TeradataConnection.go:844"),
            ("(*TeradataRows).processResponseBundle", "TeradataRows.go:2316"),
            ("(*TeradataRows).loadResponseBundle", "TeradataRows.go:2178"),
            ("newTeradataRows", "TeradataRows.go:651"), ("(*teradataStatement).QueryContext", "TeradataStatement.go:123"),
            ("(*teradataConnection).QueryContext", "TeradataConnection.go:1356"),
            ("database/sql.ctxDriverQuery", "ctxutil.go:48"), ("database/sql.(*DB).queryDC.func1", "sql.go:1776"),
            ("database/sql.withLock", "sql.go:3530"), ("database/sql.(*DB).queryDC", "sql.go:1771"),
            ("database/sql.(*Conn).QueryContext", "sql.go:2027"), ("main.goCreateRows", "goside.go:652"),
            ("_cgoexp_4b80f98a9111_goCreateRows", "_cgo_gotypes.go:357"),
        ]
    )
    return OperationalError(
        f"[Version {VERSION}] [Session {session}] [{source}] [Error {code}] {message}\n{trace}"
    )


def _database():
    """One shared in-memory database: the data plus the DBC dictionary views."""
    global _db
    with _db_lock:
        if _db is not None:
            return _db
        db = duckdb.connect(":memory:")
        for schema in ("sales", "finance", "hr", "dbc"):
            db.execute(f"CREATE SCHEMA {schema}")
        db.execute("""
            CREATE TABLE sales.orders (order_id INTEGER, customer_id INTEGER, order_date DATE,
                amount DECIMAL(12,2), status VARCHAR, note VARCHAR);
            INSERT INTO sales.orders VALUES
                (1, 10, DATE '2026-09-03', 120.50, 'shipped', 'first order'),
                (2, 10, DATE '2026-10-11', 80.00, 'shipped', NULL),
                (3, 11, DATE '2026-10-20', 310.25, 'open', 'rush'),
                (4, 12, DATE '2026-11-01', 45.00, 'cancelled', NULL),
                (5, 11, DATE '2026-11-02', 99.99, 'open', NULL);
            CREATE TABLE sales.customers (customer_id INTEGER, name VARCHAR, region VARCHAR, signup_ts TIMESTAMP);
            INSERT INTO sales.customers VALUES
                (10, 'Acme', 'EMEA', TIMESTAMP '2025-01-02 09:00:00'),
                (11, 'Globex', 'AMER', TIMESTAMP '2025-03-04 10:30:00'),
                (12, 'Initech', 'APAC', TIMESTAMP '2025-06-07 08:15:00');
            CREATE TABLE sales.order_events (event_id BIGINT, order_id INTEGER, valid_period VARCHAR);
            INSERT INTO sales.order_events VALUES (100, 1, '(2026-09-03, 2026-09-05)');
            CREATE TABLE sales.slow_scan (id INTEGER);
            CREATE VIEW sales.v_monthly_revenue AS
                SELECT CAST(date_trunc('month', o.order_date) AS DATE) AS month_start, c.region,
                       CAST(SUM(o.amount) AS DECIMAL(18,2)) AS revenue
                FROM sales.orders o JOIN sales.customers c ON o.customer_id = c.customer_id
                GROUP BY 1, 2;
            CREATE TABLE finance.product_sales (product_id INTEGER, region VARCHAR, revenue DECIMAL(18,2), units INTEGER);
            INSERT INTO finance.product_sales VALUES (1, 'EMEA', 1000.00, 10), (2, 'EMEA', 500.00, 4),
                (1, 'AMER', 800.00, 7), (3, 'APAC', 120.00, 1);
            CREATE TABLE hr.salaries (employee_id INTEGER, salary DECIMAL(12,2));
            INSERT INTO hr.salaries VALUES (1, 100000.00);
        """)
        name = "VARCHAR COLLATE NOCASE"
        db.execute(f"""
            CREATE TABLE dbc.TablesV (DatabaseName {name}, TableName {name}, TableKind VARCHAR,
                CommentString VARCHAR);
            CREATE TABLE dbc.ColumnsV (DatabaseName {name}, TableName {name}, ColumnName {name},
                ColumnType VARCHAR, ColumnLength INTEGER, DecimalTotalDigits SMALLINT,
                DecimalFractionalDigits SMALLINT, CharType SMALLINT, ColumnUDTName VARCHAR,
                CommentString VARCHAR, ColumnId SMALLINT);
            CREATE TABLE dbc.ColumnsQV AS SELECT * FROM dbc.ColumnsV;
            CREATE TABLE dbc.IndicesV (DatabaseName {name}, TableName {name}, IndexNumber SMALLINT,
                IndexType VARCHAR, UniqueFlag VARCHAR, ColumnName {name}, ColumnPosition SMALLINT);
        """)
        for database, obj, kind, comment, columns, pi in _CATALOG:
            db.execute("INSERT INTO dbc.TablesV VALUES (?, ?, ?, ?)", [database, obj, kind, comment])
            for i, (col, code, length, total, frac, chartype, udt, ccomment) in enumerate(columns, 1):
                # ColumnType is CHAR(2): single-letter codes come back space-padded.
                full = [database, obj, col, code.ljust(2), length, total, frac, chartype, udt, ccomment, 1024 + i]
                db.execute("INSERT INTO dbc.ColumnsQV VALUES (?,?,?,?,?,?,?,?,?,?,?)", full)
                if kind == "V":
                    # DBC.ColumnsV reports view columns without a type.
                    full = [database, obj, col, None, None, None, None, None, None, ccomment, 1024 + i]
                db.execute("INSERT INTO dbc.ColumnsV VALUES (?,?,?,?,?,?,?,?,?,?,?)", full)
            if pi:
                itype, unique, cols = pi
                for pos, col in enumerate(cols, 1):
                    db.execute("INSERT INTO dbc.IndicesV VALUES (?,?,?,?,?,?,?)",
                               [database, obj, 1, itype, unique, col, pos])
        for database, obj in _JOIN_INDEXES:
            db.execute("INSERT INTO dbc.TablesV VALUES (?, ?, 'I', NULL)", [database, obj])
        allowed = ", ".join(f"'{d}'" for d in _ACCESSIBLE_DATABASES)
        for view in ("TablesV", "ColumnsV", "ColumnsQV", "IndicesV"):
            db.execute(f"CREATE VIEW dbc.{view}X AS SELECT * FROM dbc.{view} WHERE lower(DatabaseName) IN ({allowed})")
        _db = db
        return db


def _objects():
    return {(d, o): (kind, columns) for d, o, kind, _c, columns, _pi in _CATALOG}


_READ_VERB = re.compile(r"^\s*(SELECT|SEL|WITH|HELP|SHOW|EXPLAIN|\()", re.IGNORECASE)
_LOCK_PREFIX = re.compile(
    r"^\s*LOCK(?:ING)?\s+(?:(?:DATABASE|TABLE|VIEW|ROW)\s+)?(?:[\w.\"]+\s+)?(?:FOR|IN)\s+\w+(?:\s+MODE)?\s*",
    re.IGNORECASE,
)
_HELP_COLUMN = re.compile(r'^\s*HELP\s+COLUMN\s+"?([\w$#]+)"?\s*\.\s*"?([\w$#]+)"?\s*\.\s*\*\s*;?\s*$', re.IGNORECASE)
_DBC = re.compile(r"\bDBC\s*\.", re.IGNORECASE)


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self.rowcount = -1
        self.arraysize = 1
        self._rows = []

    def execute(self, sql, params=None, ignoreErrors=None):
        if params is not None and not isinstance(params, (list, tuple)):
            raise TypeError(f"params unexpected type {type(params)}")
        self.connection._check_open()
        params = list(params or [])
        _bump(requests=1, active=1, statements=sql[:200])
        try:
            self._dispatch(sql, params)
        finally:
            _bump(active=-1)
        return self

    def _dispatch(self, sql, params):
        sid = self.connection.session
        if not _READ_VERB.match(_LOCK_PREFIX.sub("", sql, count=1)):
            # Nothing that is not a read should ever get here: the client's
            # read-only guard refuses it first. Record it for the tests.
            _bump(writes_seen=sql[:200])
            raise _error(3523, "The user does not have the required access.", sid)
        sql = _LOCK_PREFIX.sub("", sql, count=1)
        m = _HELP_COLUMN.match(sql)
        if m:
            return self._help_column(m.group(1), m.group(2), sid)
        if _DBC.search(sql):
            if re.search(r"\bColumnsQVX?\b", sql, re.IGNORECASE) and not qvci_enabled():
                raise _error(9719, "QVCI feature is disabled.", sid)
            return self._run_duckdb(sql, params, sid)
        return self._data_query(sql, params, sid)

    def _help_column(self, database, obj, sid):
        found = _objects().get((database.lower(), obj.lower()))
        if found is None:
            raise _error(3807, f"Object '{database}.{obj}' does not exist.", sid)
        if database.lower() not in _ACCESSIBLE_DATABASES:
            raise _error(3523, f"The user does not have any access to {database}.{obj}.", sid)
        if obj.lower() == "v_broken":
            raise _error(3807, "Object 'finance.dropped_table' does not exist.", sid)
        kind, columns = found
        titles = ["Column Name", "Type", "Nullable", "Format", "Max Length", "Decimal Total Digits",
                  "Decimal Fractional Digits", "Char Type", "UDT Name", "Column Dictionary Name",
                  "UDT Dictionary Name"]
        self.description = [(t, str, None, None, None, None, True) for t in titles]
        self._rows = [
            (c[0], c[1].ljust(2), "Y", "X(20)", c[2], c[3], c[4], c[5], c[6], c[0], c[6])
            for c in columns
        ]
        self.rowcount = len(self._rows)

    def _data_query(self, sql, params, sid):
        upper = re.sub(r"'(?:[^']|'')*'", "''", sql).upper()
        if re.search(r"\bLIMIT\b", upper):
            raise _error(3706, "Syntax error: expected something between a word and the 'LIMIT' keyword.", sid)
        if re.search(r"\bILIKE\b", upper):
            raise _error(3706, "Syntax error: expected something between a word and the 'ILIKE' keyword.", sid)
        if re.search(r"\bSEL(?:ECT)?\s+TOP\b", upper) and re.search(r"\bQUALIFY\b", upper):
            raise _error(3706, "Syntax error: Top N option is not supported with QUALIFY clause.", sid)
        try:
            tree = sqlglot.parse_one(sql, read="teradata")
        except Exception as e:
            raise _error(3706, f"Syntax error: {str(e).splitlines()[0]}", sid)
        for table in tree.find_all(sqlglot.exp.Table):
            database = (table.db or "").lower()
            name = table.name.lower()
            if not database:
                continue
            if (database, name) not in _objects():
                raise _error(3807, f"Object '{table.db}.{table.name}' does not exist.", sid)
            if database not in _ACCESSIBLE_DATABASES:
                raise _error(3523, f"The user does not have SELECT access to {table.db}.{table.name}.", sid)
            if name == "slow_scan":
                return self._slow(sid)
        return self._run_duckdb(tree.sql(dialect="duckdb"), params, sid)

    def _slow(self, sid):
        deadline = time.monotonic() + 30
        event = self.connection._cancel_event
        event.clear()
        while time.monotonic() < deadline:
            if event.wait(0.05):
                _bump(cancels=1)
                raise _error(3110, "The transaction was aborted by the user.", sid)
        self.description = [("id", int, None, None, None, None, True)]
        self._rows = []

    def _run_duckdb(self, sql, params, sid):
        cur = self.connection._duck
        try:
            res = cur.execute(sql, params) if params else cur.execute(sql)
            self.description = res.description
            self._rows = res.fetchall()
        except duckdb.Error as e:
            raise _error(3706, f"Syntax error: {str(e).splitlines()[0]}", sid)
        self.rowcount = len(self._rows)

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchmany(self, size=None):
        size = size or self.arraysize
        rows, self._rows = self._rows[:size], self._rows[size:]
        return rows

    def nextset(self):
        return None

    def setinputsizes(self, sizes):
        pass

    def setoutputsize(self, size, column=None):
        pass

    def close(self):
        self._rows = []


class TeradataConnection:
    def __init__(self, params):
        self.params = params
        self.session = next(_sessions)
        self.autocommit = True
        self._closed = False
        self._cancel_event = threading.Event()
        self._duck = _database().cursor()

    def _check_open(self):
        if self._closed:
            raise _error(1095, "Cannot operate on a closed Connection.", self.session, "Teradata SQL Driver")

    def cursor(self):
        self._check_open()
        return Cursor(self)

    def cancel(self):
        """Abort the request running on this connection (callable from any thread)."""
        self._cancel_event.set()
        with contextlib.suppress(Exception):
            self._duck.interrupt()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        if not self._closed:
            self._closed = True
            _bump(logoffs=1)


def connect(sConnectParams=None, **kwargs):
    params = {}
    if sConnectParams:
        params.update(json.loads(sConnectParams))
    params.update(kwargs)
    unknown = [k for k in params if k not in KNOWN_PARAMS]
    if unknown:
        raise _error(169, "[SQLState 08000] Unable to parse JSON connection parameters",
                     0, "Teradata SQL Driver")
    host = str(params.get("host", ""))
    if host in UNREACHABLE_HOSTS:
        raise _error(493, f"[SQLState 08000] Hostname lookup failed for {host}", 0, "Teradata SQL Driver")
    user, password = params.get("user"), params.get("password")
    if USERS.get(user) != password:
        raise _error(8017, "The UserId, Password or Account is invalid.")
    database = params.get("database")
    if database and str(database).lower() not in {d for d, *_ in _CATALOG}:
        raise _error(3802, f"Database '{database}' does not exist.")
    conn = TeradataConnection(params)
    _bump(logons=1, last_logon={k: ("***" if k == "password" else v) for k, v in params.items()})
    return conn


class _VerNumber:
    sVersionNumber = VERSION


vernumber = _VerNumber()
