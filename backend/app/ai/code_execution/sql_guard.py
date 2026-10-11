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
    blocklist over the text with string literals and comments removed.
  * **Splunk**: SPL is not SQL but can write; its writing commands are refused.
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
}

# Statements sqlglot leaves as an opaque Command that are still reads.
_READ_COMMANDS = {"SHOW", "DESCRIBE", "DESC", "EXPLAIN", "PRAGMA", "USE"}

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
    dialect = CLIENT_DIALECTS.get(name)
    if dialect:
        check_sql(query, dialect)


def check_sql(sql: str, dialect: str) -> None:
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import SqlglotError

    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except SqlglotError as e:
        logger.warning("sql_guard: %s parser could not read query, using keyword check: %s",
                       dialect, str(e)[:200])
        _keyword_check(sql)
        return

    if len(statements) != 1:
        raise ReadOnlyViolation(
            f"Only a single read-only statement is allowed; got {len(statements)} statements."
        )
    stmt = statements[0]

    if isinstance(stmt, exp.Command):
        verb = (stmt.this or "").strip().split(" ")[0].upper()
        if verb not in _READ_COMMANDS or (verb == "PRAGMA" and "=" in sql):
            raise ReadOnlyViolation(f"Statement '{verb}' is not allowed. Only SELECT queries are allowed.")
        if verb == "EXPLAIN":
            # EXPLAIN ANALYZE executes its statement on Postgres/MySQL.
            _keyword_check(getattr(stmt.expression, "this", "") or "")
        return

    reads = (exp.Select, exp.SetOperation, exp.Subquery, exp.Describe, exp.Show, exp.Pragma)
    if not isinstance(stmt, reads):
        raise ReadOnlyViolation(
            f"Statement '{stmt.key.upper()}' is not allowed. Only SELECT queries are allowed."
        )
    if isinstance(stmt, exp.Pragma) and "=" in sql:
        raise ReadOnlyViolation("PRAGMA assignments are not allowed.")

    writes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop,
              exp.Alter, exp.TruncateTable, exp.Command, exp.Into, exp.Copy)
    for node in stmt.walk():
        if isinstance(node, writes):
            raise ReadOnlyViolation(
                f"Query contains a write operation ('{node.key.upper()}'). Only SELECT queries are allowed."
            )


def _keyword_check(sql: str) -> None:
    match = _WRITE_KEYWORDS.search(_LITERALS_AND_COMMENTS.sub(" ", sql))
    if match:
        raise ReadOnlyViolation(
            f"Query contains forbidden operation '{match.group().upper()}'. Only SELECT queries are allowed."
        )


def _check_spl(spl: str) -> None:
    match = _SPL_WRITE_COMMANDS.search(spl)
    if match:
        raise ReadOnlyViolation(
            f"SPL command '{match.group(1)}' writes data and is not allowed."
        )
