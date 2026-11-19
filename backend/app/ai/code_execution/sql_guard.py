"""Read-only guard for SQL that reaches a data source.

Every query an agent runs, and every custom-table definition an admin saves, is
checked here on the *final* text — after the sandbox code built it and after
`params` were rendered — so string tricks in generated Python (`"DEL" + "ETE"`,
`bytes.fromhex(...)`, `" ".join(...)`) are no way around it. The AST scan of the
sandbox code still runs first; this is the check that sees what actually runs.

Policy, per client:

  * **A client whose SQL dialect sqlglot knows** (Postgres, MySQL, SQL Server,
    Snowflake, BigQuery, ...): parse it and allow exactly one read statement —
    SELECT / set operation / WITH ... SELECT, plus SHOW / DESCRIBE / EXPLAIN.
    Anything that writes anywhere in the tree (a DELETE inside a CTE,
    `SELECT ... INTO`) is refused. If sqlglot cannot parse the text, we do not
    block a possibly-valid read on a parser gap; we fall back to the keyword
    blocklist over the text with string literals and comments removed, and
    refuse a second statement after a `;`.
  * **DuckDB running inside the API process** (duckdb, csv, qvd, pbix,
    iceberg, network-dir tables): as above, plus the statement may not name a
    file or URL itself — no file-reading table function, no path used as a
    table. A connection's files are reached through its views. Text sqlglot
    cannot parse is not waved through: DuckDB's own parser must see exactly
    one SELECT.
  * **Splunk**: SPL is not SQL but can write; its writing commands are refused.
  * **Db2** (LUW, z/OS, IBM i): sqlglot has no Db2 dialect. One statement, a
    read verb (SELECT / WITH / VALUES) and no write keyword, generic or Db2's.
  * **Everything else** (Salesforce SOQL, PostHog HogQL, Mongo, Qlik, ...): not
    checked here. Their query APIs are read-only, and connections are expected
    to use read-only credentials regardless.

This is defence in depth, not a substitute for read-only database accounts:
a SELECT can still call a function with side effects.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)
# sqlglot warns on every statement it keeps as an opaque Command (SHOW, EXPLAIN).
logging.getLogger("sqlglot").setLevel(logging.ERROR)


class ReadOnlyViolation(ValueError):
    """The statement would modify the source."""


# Client class name -> sqlglot dialect. Keyed by class name rather than by
# import so this module stays importable without every driver installed.
CLIENT_DIALECTS = {
    "PostgresqlClient": "postgres",
    "AwsRedshiftClient": "redshift",
    "MysqlClient": "mysql",
    "MariadbClient": "mysql",
    "MSSQLClient": "tsql",
    "MsFabricClient": "fabric",
    "OracledbClient": "oracle",
    "SqliteClient": "sqlite",
    "SnowflakeClient": "snowflake",
    "BigqueryClient": "bigquery",
    "AwsAthenaClient": "athena",
    "PrestoClient": "presto",
    "TrinoClient": "trino",
    "ClickhouseClient": "clickhouse",
    "DatabricksSqlClient": "databricks",
    "SparkConnectClient": "spark",
    "DuckDBClient": "duckdb",
    "TeradataClient": "teradata",
    "DruidClient": "druid",
    "IcebergClient": "duckdb",
    "PostgresqlMultiClient": "duckdb",
    "DremioClient": "dremio",
    "HiveClient": "hive",
}

# Connectors that run DuckDB SQL in-process but are deliberately absent from
# CLIENT_DIALECTS: that map also switches on table-scope and incremental-window
# handling, which these connectors have never had. Only the read-only check
# applies to them.
_IN_PROCESS_DUCKDB_CLIENTS = {"CSVClient", "QVDClient", "PBIXClient"}

# DuckDB table functions that open a file, URL or another database by name.
_DUCKDB_FILE_FUNCTIONS = re.compile(
    r"^(?:read_\w+|\w+_scan|scan_\w+|glob|sniff_csv|from_csv_auto|parquet_\w+|iceberg_\w+"
    r"|delta_\w+|st_read\w*|st_drivers|shapefile_meta|load_\w+|import_database|export_database"
    r"|copy_database|sqlite_attach|postgres_\w+|mysql_\w+|query|query_table|json_execute_serialized_sql"
    r"|duckdb_logs_parsed)$",
    re.IGNORECASE,
)
# The same, for text that could not be parsed into a tree.
_DUCKDB_FILE_TEXT = re.compile(
    r"\b(?:read_\w+|\w+_scan|scan_\w+|glob|sniff_csv|from_csv_auto|parquet_\w+|iceberg_\w+"
    r"|delta_\w+|st_read\w*|shapefile_meta|load_\w+|import_database|export_database"
    r"|copy_database|sqlite_attach|postgres_\w+|mysql_\w+|query|query_table"
    r"|json_execute_serialized_sql)\s*\(",
    re.IGNORECASE,
)
# Which relations a statement may name is decided from the engine's own parse
# tree (`_duckdb_lockdown.engine_tree_check`), not here: only it tells a
# string from an identifier the way DuckDB does.

# PRAGMAs that take an argument and only report: the argument names an object.
_READ_PRAGMAS_WITH_ARGUMENT = {
    "table_info", "table_xinfo", "table_list", "index_list", "index_info", "index_xinfo",
    "foreign_key_list", "foreign_key_check", "integrity_check", "quick_check",
    "show", "storage_info",
}
_PRAGMA_CALL = re.compile(r"^\s*PRAGMA\s+(?:[\w\"`\[\]]+\s*\.\s*)?(\w+)\s*\(", re.IGNORECASE)

# Statements sqlglot leaves as an opaque Command that are still reads.
_READ_COMMANDS = {"SHOW", "DESCRIBE", "DESC", "EXPLAIN", "PRAGMA"}

_WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|EXEC|EXECUTE"
    r"|MERGE|CALL|REPLACE\s+INTO|UPSERT|LOAD|COPY|VACUUM|ATTACH|DETACH|INTO\s+OUTFILE"
    r"|INTO\s+DUMPFILE|SET)\b",
    re.IGNORECASE,
)
_LITERALS_AND_COMMENTS = re.compile(
    r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`|\[[^\]]*\]|--[^\n]*|/\*.*?\*/",
    re.DOTALL,
)

# SPL commands that write to indexes, lookups, the filesystem, or the outside.
_SPL_WRITE_COMMANDS = re.compile(
    r"\|\s*(delete|outputlookup|outputcsv|outputtext|collect|mcollect|meventcollect"
    r"|tscollect|sendemail|sendalert|script|run|dbxoutput|dbxquery)\b",
    re.IGNORECASE,
)


# Teradata lets a read start with lock clauses: LOCKING [DATABASE|TABLE|VIEW|ROW]
# [name] FOR|IN ACCESS|READ|SHARE [MODE] [NOWAIT]. ACCESS (dirty read) is the
# usual reporting idiom. WRITE / EXCLUSIVE locks would block the source's own
# loads, so a read-only agent never takes them.
_TERADATA_LOCK_PREFIX = re.compile(
    r"^\s*LOCK(?:ING)?\s+(?:(?:DATABASE|TABLE|VIEW|ROW)\s+)?(?:[\w.\"$#]+\s+)?(?:FOR|IN)\s+"
    r"(?P<mode>\w+)(?:\s+MODE)?(?:\s+NOWAIT)?\s*",
    re.IGNORECASE,
)
_TERADATA_READ_LOCKS = {"ACCESS", "READ", "SHARE"}
_TERADATA_CHARSET_CLAUSE = re.compile(r"\bCHARACTER\s+SET\s+\w+", re.IGNORECASE)
# First word of a statement Teradata runs without changing anything.
_TERADATA_READ_VERBS = {"SELECT", "SEL", "WITH", "HELP", "SHOW", "EXPLAIN", "("}


def _check_teradata(sql: str) -> str:
    """Teradata-only pre-check: the leading verb must be a read.

    An allowlist on purpose. Teradata has short forms and statements sqlglot
    cannot parse (CT = CREATE TABLE, CV = CREATE VIEW, MODIFY USER, GIVE,
    COMMENT ON ... AS), and unparsed text only gets the generic keyword check,
    which does not know them. None of them starts with a read verb.
    Returns the statement with its lock clauses removed.
    """
    text = _LITERALS_AND_COMMENTS.sub(lambda m: m.group() if m.group().startswith(("'", '"')) else " ", sql)
    body = text
    while True:
        m = _TERADATA_LOCK_PREFIX.match(body)
        if not m:
            break
        mode = m.group("mode").upper()
        if mode not in _TERADATA_READ_LOCKS:
            raise ReadOnlyViolation(
                f"LOCKING ... FOR {mode} is not allowed. Use LOCKING ROW FOR ACCESS for reads."
            )
        body = body[m.end():]
    stripped = body.lstrip()
    verb = "(" if stripped.startswith("(") else (stripped.split(None, 1)[0].upper() if stripped else "")
    if verb not in _TERADATA_READ_VERBS:
        raise ReadOnlyViolation(
            f"Statement '{verb or '(empty)'}' is not allowed. Only SELECT queries are allowed."
        )
    return body


# Db2 (LUW, z/OS, IBM i) has no sqlglot dialect, so its reads are checked by
# an allowlist on the leading verb plus the keyword blocklist, like Teradata's
# unparsed path. Keyed by class name so this module needs no Db2 driver.
_DB2_CLIENTS = {"Db2Client", "IbmiClient"}
_DB2_READ_VERBS = {"SELECT", "WITH", "VALUES", "("}
# Db2-specific statements and routines that change state. QCMDEXC runs an IBM i
# CL command and is callable as a scalar function inside a SELECT; ADMIN_CMD
# runs Db2 LUW administrative commands.
_DB2_WRITE_KEYWORDS = re.compile(
    r"\b(LOCK|REFRESH|DECLARE|RENAME|COMMENT|LABEL|QCMDEXC|ADMIN_CMD|SYSPROC|EXPORT|IMPORT"
    r"|RUNSTATS|REORG|TRANSFER|SAVEPOINT|ROLLBACK|COMMIT|RELEASE|CONNECT|DISCONNECT)\b",
    re.IGNORECASE,
)


def _check_db2(sql: str) -> None:
    """Db2 read-only check: one statement, a read verb, no write keyword.

    `SELECT ... FROM FINAL TABLE (INSERT ...)` (a data-change table reference)
    starts with SELECT but is caught by the INSERT/UPDATE/DELETE/MERGE keywords.
    """
    _single_statement_check(sql)
    text = _LITERALS_AND_COMMENTS.sub(" ", sql).strip().rstrip(";").strip()
    verb = "(" if text.startswith("(") else (text.split(None, 1)[0].upper() if text else "")
    if verb not in _DB2_READ_VERBS:
        raise ReadOnlyViolation(
            f"Statement '{verb or '(empty)'}' is not allowed. Only SELECT queries are allowed."
        )
    _keyword_check(sql)
    match = _DB2_WRITE_KEYWORDS.search(text)
    if match:
        raise ReadOnlyViolation(
            f"Query contains forbidden operation '{match.group().upper()}'. Only SELECT queries are allowed."
        )


def dialect_for_client(client) -> Optional[str]:
    return CLIENT_DIALECTS.get(type(client).__name__)


def ensure_read_only(client, query) -> None:
    """Raise ReadOnlyViolation if `query` would modify the source behind `client`."""
    if not isinstance(query, str) or not query.strip():
        return
    name = type(client).__name__
    if name == "SplunkClient":
        _check_spl(query)
        return
    if name in _DB2_CLIENTS:
        _check_db2(query)
        return
    from app.data_sources.clients.neo4j_client import Neo4jClient

    if isinstance(client, Neo4jClient):
        from app.data_sources.clients.cypher_guard import CypherNotAllowed, check_cypher

        try:
            check_cypher(query, allow_gds=client.allow_gds)
        except CypherNotAllowed as e:
            raise ReadOnlyViolation(str(e)) from e
        return
    dialect = CLIENT_DIALECTS.get(name)
    if not dialect and name in _IN_PROCESS_DUCKDB_CLIENTS and not _file_connectors_unrestricted():
        dialect = "duckdb"
    if dialect:
        check_sql(query, dialect,
                  relations_checked_by_client=name in _IN_PROCESS_DUCKDB_CLIENTS or name == "DuckDBClient")


def _file_connectors_unrestricted() -> bool:
    from app.data_sources.clients._duckdb_lockdown import unrestricted

    return unrestricted()


def check_sql(sql: str, dialect: str, *, relations_checked_by_client: bool = False) -> None:
    """``relations_checked_by_client``: the DuckDB connector that will run the
    statement checks every relation it names against its own catalog first
    (a quoted schema or table name can hold any character, and only the
    session knows which names are real), so an identifier this check cannot
    place is left to it instead of being refused here."""
    from app.ai.code_execution.sql_parser import parse_guard_sql
    from sqlglot import exp
    from sqlglot.errors import SqlglotError

    teradata = dialect == "teradata"
    # DuckDB here always means an engine inside the API process.
    in_process = dialect == "duckdb" and not _file_connectors_unrestricted()
    if in_process:
        # The engine's own parser has the first word on what the statement
        # is and what it names: relations must be tables (never a string,
        # path or URL), table functions come from a short built-in list, and
        # nothing reads settings or secrets. sqlglot does not see dollar-quoted
        # or E'' strings, quoted function names or comment-split calls the way
        # DuckDB does; this does.
        from app.data_sources.clients._duckdb_lockdown import engine_tree_check

        engine_tree_check(sql, deferred=relations_checked_by_client)
    if teradata:
        _check_teradata(sql)

    try:
        # A comment after the final `;` parses as a bare Semicolon node holding
        # only that comment; it runs nothing, so it is not a second statement.
        statements = [s for s in parse_guard_sql(sql, dialect)
                      if s is not None and not isinstance(s, exp.Semicolon)]
    except SqlglotError as e:
        logger.warning("sql_guard: %s parser could not read query, using keyword check: %s",
                       dialect, str(e)[:200])
        if in_process:
            _check_unparsed_duckdb(sql)
        elif teradata:
            # `CHARACTER SET UNICODE` in a CAST is a read; without this the
            # keyword check would see SET. The leading-verb allowlist above
            # already ran; what is left is a second statement smuggled after a
            # `;` (Teradata multi-statement requests).
            _keyword_check(_TERADATA_CHARSET_CLAUSE.sub(" ", sql))
            _single_statement_check(sql)
        else:
            _keyword_check(sql)
            _single_statement_check(sql)
        return

    if len(statements) != 1:
        raise ReadOnlyViolation(
            f"Only a single read-only statement is allowed; got {len(statements)} statements."
        )
    stmt = statements[0]

    if teradata and type(stmt).__name__ == "LockingStatement":
        # The lock mode was checked by _check_teradata; what it wraps must
        # itself be a read.
        lock = stmt.this
        mode = str(lock.args.get("lock_type") or "").upper() if lock is not None else ""
        if mode not in _TERADATA_READ_LOCKS:
            raise ReadOnlyViolation(f"LOCKING ... FOR {mode or '?'} is not allowed.")
        stmt = stmt.expression
        if stmt is None:
            raise ReadOnlyViolation("LOCKING must be followed by a SELECT statement.")

    if isinstance(stmt, exp.Command):
        verb = (stmt.this or "").strip().split(" ")[0].upper()
        if teradata and verb == "HELP":
            # HELP TABLE / COLUMN / VIEW / SESSION only report metadata.
            return
        if verb not in _READ_COMMANDS or (verb == "PRAGMA" and "=" in sql):
            raise ReadOnlyViolation(f"Statement '{verb}' is not allowed. Only SELECT queries are allowed.")
        if verb == "PRAGMA":
            _check_pragma(sql)
        if verb == "EXPLAIN":
            # EXPLAIN ANALYZE executes its statement on Postgres/MySQL.
            _keyword_check(getattr(stmt.expression, "this", "") or "")
        if in_process:
            # No tree to walk for an opaque command; EXPLAIN ANALYZE runs its
            # statement here too.
            _check_duckdb_text(sql)
        return

    if isinstance(stmt, exp.Use):
        # Picking a compute warehouse changes no data or privileges. USE ROLE /
        # DATABASE / SCHEMA could widen what reads can reach, so they stay refused.
        kind = stmt.args.get("kind")
        kind = kind.name if isinstance(kind, exp.Expression) else (kind or "")
        if kind.upper() == "WAREHOUSE":
            return
        raise ReadOnlyViolation("Only 'USE WAREHOUSE' is allowed. Only SELECT queries are allowed.")

    reads = (exp.Select, exp.SetOperation, exp.Subquery, exp.Describe, exp.Show, exp.Pragma)
    if in_process:
        # DuckDB statement forms of a read; its tree is still walked below.
        reads += (exp.Summarize, exp.Pivot, exp.Values)
    if not isinstance(stmt, reads):
        raise ReadOnlyViolation(
            f"Statement '{stmt.key.upper()}' is not allowed. Only SELECT queries are allowed."
        )
    if isinstance(stmt, exp.Pragma):
        if "=" in sql:
            raise ReadOnlyViolation("PRAGMA assignments are not allowed.")
        _check_pragma(sql)

    writes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop,
              exp.Alter, exp.TruncateTable, exp.Command, exp.Into, exp.Copy)
    for node in stmt.walk():
        if isinstance(node, writes):
            raise ReadOnlyViolation(
                f"Query contains a write operation ('{node.key.upper()}'). Only SELECT queries are allowed."
            )
    if in_process:
        _check_duckdb_tree(stmt)


_FILE_ACCESS_REFUSED = (
    "Reading files or URLs directly is not allowed. Query the connection's tables by name."
)


def _check_pragma(sql: str) -> None:
    """`PRAGMA name(arg)` sets `name` on SQLite just as `PRAGMA name = arg`
    does. Only the ones whose argument names an object to describe pass."""
    text = _LITERALS_AND_COMMENTS.sub(lambda m: m.group() if not m.group().startswith(("--", "/*")) else " ", sql)
    m = _PRAGMA_CALL.match(text)
    if m and m.group(1).lower() not in _READ_PRAGMAS_WITH_ARGUMENT:
        raise ReadOnlyViolation(f"PRAGMA {m.group(1)}(...) is not allowed.")


def _check_duckdb_tree(stmt) -> None:
    from sqlglot import exp

    for node in stmt.walk():
        if isinstance(node, exp.Func):
            name = node.name if isinstance(node, exp.Anonymous) else node.sql_name()
            if _DUCKDB_FILE_FUNCTIONS.match(name or ""):
                raise ReadOnlyViolation(_FILE_ACCESS_REFUSED)


def _check_duckdb_text(sql: str) -> None:
    match = _DUCKDB_FILE_TEXT.search(_LITERALS_AND_COMMENTS.sub(" ", sql))
    if match:
        raise ReadOnlyViolation(_FILE_ACCESS_REFUSED)


def _check_unparsed_duckdb(sql: str) -> None:
    """sqlglot could not read it. The engine's own parser decides what it is:
    exactly one SELECT (DESCRIBE / SHOW / SUMMARIZE count as SELECT there)."""
    try:
        import duckdb

        statements = duckdb.extract_statements(sql)
    except Exception as e:
        raise ReadOnlyViolation("The statement could not be parsed. Only SELECT queries are allowed.") from e
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        raise ReadOnlyViolation("Only a single read-only SELECT statement is allowed.")
    _check_duckdb_text(sql)


def _keyword_check(sql: str) -> None:
    match = _WRITE_KEYWORDS.search(_LITERALS_AND_COMMENTS.sub(" ", sql))
    if match:
        raise ReadOnlyViolation(
            f"Query contains forbidden operation '{match.group().upper()}'. Only SELECT queries are allowed."
        )


def _single_statement_check(sql: str) -> None:
    """Unparsed text: refuse anything after a `;` other than whitespace."""
    parts = _LITERALS_AND_COMMENTS.sub(" ", sql).split(";")
    if any(p.strip() for p in parts[1:]):
        raise ReadOnlyViolation("Only a single read-only statement is allowed.")


def _check_spl(spl: str) -> None:
    match = _SPL_WRITE_COMMANDS.search(spl)
    if match:
        raise ReadOnlyViolation(
            f"SPL command '{match.group(1)}' writes data and is not allowed."
        )
