"""DuckDB SQL read the way DuckDB reads it.

`inspect_duckdb_sql` hands the text to DuckDB's own parser
(`json_serialize_sql`) on a private in-memory connection that has external
access off and its configuration locked. Serializing only parses: nothing is
bound, so no file, extension or address named in the text is opened. Only a
single SELECT serializes; anything else is refused.

`check_duckdb_file_query` is the rule for connections over local files
(CSV, QVD, PBIX, a Power BI Report Server report's model): one SELECT that
reads only the connection's own tables (or its own CTEs, where they are in
scope), calls no file reader, settings or catalog function, and uses no
table function but a few generators. The client runs it on the connection
it then executes on, after that connection is locked
(`app.data_sources.clients._duckdb_lockdown`).

Two kinds of refusal: a policy refusal (`ReadOnlyViolation`,
`TableScopeViolation`) ends the attempt; `DuckdbQueryError` means the text is
wrong in a way the agent fixes by rewriting it (it does not parse, asks for
DESCRIBE / SHOW, or names a CTE oddly), so the code loop retries it like any
query error. A statement gets that fixable error only when no policy refuses
any part of it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass

import duckdb

from app.ai.code_execution.sql_guard import ReadOnlyViolation
from app.ai.code_execution.table_scope import TableScopeViolation
from app.errors.redact import describe_for_log

logger = logging.getLogger(__name__)


class DuckdbQueryError(ValueError):
    """The text is not a query this connection runs, for a reason the agent
    fixes by rewriting it. Nothing ran and no policy refused it: retryable,
    unlike ReadOnlyViolation and TableScopeViolation."""


class DuckdbSyntaxError(DuckdbQueryError):
    """DuckDB's parser could not read the text; the message is its reason."""


class ConnectionTableViolation(TableScopeViolation):
    """The query reads a table the connection itself does not have. File
    connections have no table selection: the refusal is about their own
    tables, and the message lists them."""

    refused_by = "connection"


@dataclass(frozen=True)
class DuckdbStatement:
    """What one SELECT reads. Names are lowercased; a table is
    (catalog, schema, name), with "" for a part the text leaves out. A name
    that is one of the statement's CTEs where it stands is not a table;
    `cte_names` lists every CTE the statement defines. `describes` is true
    when it holds a DESCRIBE, SHOW or SUMMARIZE anywhere."""

    tables: frozenset[tuple[str, str, str]]
    table_functions: frozenset[str]
    functions: frozenset[str]
    cte_names: frozenset[str]
    describes: bool = False


# One connection for every call, created on first use. The sandbox runs each
# query on a thread of its own and a DuckDB connection is not safe for
# concurrent use, so creating it and every call on it hold this lock.
_LOCK = threading.Lock()
_CONNECTION: duckdb.DuckDBPyConnection | None = None

# Errors DuckDB raises about the text itself; any other error from the
# inspector is about its connection, which is then rebuilt.
_TEXT_ERRORS = (duckdb.ParserException, duckdb.InvalidInputException, duckdb.ConversionException)

# Table references the parser emits for a plain read. Any other kind (one a
# later DuckDB adds) is refused rather than guessed at.
_READ_TABLE_REFS = frozenset({
    "BASE_TABLE", "JOIN", "SUBQUERY", "TABLE_FUNCTION", "EXPRESSION_LIST", "EMPTY", "EMPTY_FROM", "PIVOT",
})
# The root of a SELECT statement.
_QUERY_NODES = frozenset({"SELECT_NODE", "SET_OPERATION_NODE"})

_ONLY_SELECT = "Only a single SELECT query is allowed on this connection."
# The retryable messages name only structure and carry no quote, ": " or
# " (", so they survive redaction whole when data is hidden from the model
# (app.ai.data_visibility.cut_before_value).
_NO_DESCRIBE = ("DESCRIBE, SHOW and SUMMARIZE are not available on this connection. "
                "SELECT * FROM <table> LIMIT 0 gives the columns of a table.")
_TOO_DEEP = ("The query nests too deeply to run on this connection. Simplify it, with fewer nested "
             "expressions or subqueries, for example by moving parts of it into CTEs.")


def _connection() -> duckdb.DuckDBPyConnection:
    global _CONNECTION
    if _CONNECTION is None:
        _CONNECTION = duckdb.connect(
            database=":memory:",
            config={"enable_external_access": False, "lock_configuration": True},
        )
    return _CONNECTION


def _drop_connection() -> None:
    """Forget the inspector connection (caller holds _LOCK); the next call
    builds a new one."""
    global _CONNECTION
    con, _CONNECTION = _CONNECTION, None
    if con is not None:
        try:
            con.close()
        except Exception as err:
            logger.warning("duckdb_guard.inspector.close_failed %s", describe_for_log(err))


def _serialize(sql: str) -> dict:
    with _LOCK:
        try:
            raw = _connection().execute("SELECT json_serialize_sql($1)", [sql]).fetchone()[0]
        except _TEXT_ERRORS:
            raise
        except Exception:
            _drop_connection()
            raise
    tree = json.loads(raw)
    if not isinstance(tree, dict):
        raise ValueError("unexpected serialization")
    return tree


class _Reader:
    """Collects what a serialized statement reads.

    CTE scope follows DuckDB's binder: a WITH's CTEs are visible in its
    query, each CTE body sees the CTEs before it, and a recursive CTE sees
    itself only in its recursive part, where a USING KEY one also sees its
    kept rows as recurring.<name>. Visible names are counted in dicts, added
    on entering a scope and removed on leaving it, so the walk stays linear
    in the size of the tree.

    A DESCRIBE / SHOW / SUMMARIZE is noted and the walk goes on, so what
    else the statement reads is still collected."""

    def __init__(self) -> None:
        self.tables: set[tuple[str, str, str]] = set()
        self.table_functions: set[str] = set()
        self.functions: set[str] = set()
        self.cte_names: set[str] = set()
        self.describes = False
        self._visible: dict[str, int] = {}
        self._recurring: dict[str, int] = {}

    @staticmethod
    def _show(names: dict[str, int], name: str) -> None:
        names[name] = names.get(name, 0) + 1

    @staticmethod
    def _hide(names: dict[str, int], name: str) -> None:
        left = names[name] - 1
        if left:
            names[name] = left
        else:
            del names[name]

    def _is_cte(self, ref: tuple[str, str, str]) -> bool:
        catalog, schema, name = ref
        if catalog:
            return False
        if schema == "recurring":
            return name in self._recurring
        return not schema and name in self._visible

    @staticmethod
    def _ctes(node: dict) -> list[tuple[str, object]]:
        cte_map = node.get("cte_map")
        if cte_map is None:
            return []
        entries = cte_map.get("map") if isinstance(cte_map, dict) else None
        if not isinstance(entries, list):
            raise ReadOnlyViolation(_ONLY_SELECT)
        out = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
                raise ReadOnlyViolation(_ONLY_SELECT)
            out.append((entry["key"].lower(), entry.get("value")))
        return out

    def walk(self, node) -> None:
        if isinstance(node, list):
            for item in node:
                self.walk(item)
            return
        if not isinstance(node, dict):
            return
        # Table references carry an alias and a sample clause; expressions
        # carry a class; query nodes have neither alias nor class.
        if "class" not in node and "alias" in node and "sample" in node:
            kind = node.get("type")
            if kind == "SHOW_REF":
                # Its target (a table or a query) is read like any other.
                self.describes = True
            elif kind not in _READ_TABLE_REFS:
                raise ReadOnlyViolation(_ONLY_SELECT)
            if kind == "BASE_TABLE":
                ref = tuple(str(node.get(k) or "").lower() for k in ("catalog_name", "schema_name", "table_name"))
                if not self._is_cte(ref):
                    self.tables.add(ref)
            elif kind == "TABLE_FUNCTION":
                function = node.get("function")
                name = function.get("function_name") if isinstance(function, dict) else None
                if not isinstance(name, str):
                    raise ReadOnlyViolation(_ONLY_SELECT)
                self.table_functions.add(name.lower())
                # The call is a table function, not a scalar one; its
                # arguments and the rest of the node are read as usual.
                self.walk([v for k, v in function.items() if k != "function_name"])
                self.walk([v for k, v in node.items() if k != "function"])
                return
        elif "class" in node and isinstance(node.get("function_name"), str):
            self.functions.add(node["function_name"].lower())
        ctes = self._ctes(node)
        for name, body in ctes:
            self.walk(body)
            self._show(self._visible, name)
            self.cte_names.add(name)
        recursive = node.get("cte_name") if node.get("type") == "RECURSIVE_CTE_NODE" else None
        recursive = recursive.lower() if isinstance(recursive, str) else None
        keyed = recursive is not None and bool(node.get("key_targets"))
        for key, value in node.items():
            if key == "cte_map":
                continue
            if key == "right" and recursive is not None:
                self._show(self._visible, recursive)
                if keyed:
                    self._show(self._recurring, recursive)
                self.walk(value)
                if keyed:
                    self._hide(self._recurring, recursive)
                self._hide(self._visible, recursive)
            else:
                self.walk(value)
        for name, _body in ctes:
            self._hide(self._visible, name)


def inspect_duckdb_sql(sql: str) -> DuckdbStatement:
    """The tables, table functions, functions and CTE names of `sql`, which
    must be exactly one SELECT, and whether it describes. Raises
    DuckdbSyntaxError when DuckDB cannot parse it, and ReadOnlyViolation for
    anything else that is not one SELECT or cannot be read. Opens no file
    and no connection."""
    if not isinstance(sql, str):
        raise ReadOnlyViolation(_ONLY_SELECT)
    try:
        tree = _serialize(sql)
    except Exception as err:
        logger.error("duckdb_guard.inspect.failed %s", describe_for_log(err))
        raise ReadOnlyViolation(f"The query could not be read. {_ONLY_SELECT}") from err
    if tree.get("error"):
        if str(tree.get("error_type", "")).lower() == "parser":
            # The agent's own text, quoted back so it can fix it. DuckDB's
            # depth-limit hint is a SET, which this connection refuses.
            message = str(tree.get("error_message") or "The query could not be parsed.")
            raise DuckdbSyntaxError(_TOO_DEEP if "max_expression_depth" in message.lower() else message)
        raise ReadOnlyViolation(_ONLY_SELECT)
    statements = tree.get("statements")
    if not isinstance(statements, list) or len(statements) != 1:
        raise ReadOnlyViolation(_ONLY_SELECT)
    root = statements[0].get("node") if isinstance(statements[0], dict) else None
    if not isinstance(root, dict) or root.get("type") not in _QUERY_NODES:
        raise ReadOnlyViolation(_ONLY_SELECT)
    reader = _Reader()
    try:
        reader.walk(statements[0])
    except RecursionError as err:
        raise ReadOnlyViolation(f"The query is nested too deeply to check. {_ONLY_SELECT}") from err
    return DuckdbStatement(
        tables=frozenset(reader.tables),
        table_functions=frozenset(reader.table_functions),
        functions=frozenset(reader.functions),
        cte_names=frozenset(reader.cte_names),
        describes=reader.describes,
    )


# Functions that read files, settings, secrets, the environment or the
# catalog, or run SQL held in a string. The built-in macros here select from
# catalog table functions in their definition.
_DENIED_FUNCTIONS = frozenset({
    "current_setting", "getenv", "duckdb_settings", "duckdb_secrets", "duckdb_extensions",
    "query", "query_table", "glob",
    "format_type", "get_block_size", "pg_get_constraintdef", "pg_get_viewdef",
})
_ALLOWED_TABLE_FUNCTIONS = frozenset({"range", "generate_series", "unnest"})
_PLAIN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# DuckDB's built-in catalog views a bare name can reach (duckdb_tables,
# pg_settings, sqlite_master, pragma_database_list, ...).
_CATALOG_PREFIXES = ("duckdb_", "pg_", "sqlite_", "pragma_")
_LISTED_TABLES = 50


def _denied(function: str) -> bool:
    return function in _DENIED_FUNCTIONS or function.startswith("read_") or function.endswith("_scan")


def _tables_line(views: list[str]) -> str:
    if not views:
        return "This connection has no tables to query yet."
    listed = ", ".join(views[:_LISTED_TABLES])
    more = f" and {len(views) - _LISTED_TABLES} more" if len(views) > _LISTED_TABLES else ""
    return f"The tables of this connection are {listed}{more}."


def check_duckdb_file_query(sql: str, allowed_views: Iterable[str], *, not_ready: Iterable[str] = ()) -> None:
    """Refuse `sql` unless it is one SELECT reading only `allowed_views`
    (the connection's own tables, matched case-insensitively, in the default
    schema) or CTEs it defines where they are in scope, with no file reader,
    settings or catalog read, or table function other than range /
    generate_series / unnest. `not_ready` names tables the connection lists
    but could not register (their cache is not built), for the message.

    ReadOnlyViolation for the statement or a function, ConnectionTableViolation
    (a TableScopeViolation) for a table, DuckdbQueryError for text the agent
    can fix (syntax, DESCRIBE / SHOW, a CTE name that is not plain). The
    policy refusals come first: a fixable error is raised only when nothing
    in the statement is refused."""
    shown_views = sorted({str(v) for v in allowed_views}, key=str.lower)
    stmt = inspect_duckdb_sql(sql)
    for function in sorted(stmt.functions | stmt.table_functions):
        if _denied(function):
            raise ReadOnlyViolation(
                f"{function}() is not allowed on this connection. Query its tables by name.")
    for function in sorted(stmt.table_functions - _ALLOWED_TABLE_FUNCTIONS):
        raise ReadOnlyViolation(
            f"Table function {function}() is not allowed on this connection. Query its tables by name.")
    for name in sorted(stmt.cte_names):
        if name.startswith(_CATALOG_PREFIXES):
            raise ConnectionTableViolation(
                f"A CTE cannot be named {name} on this connection. Use a plain name that does not "
                f"start with {', '.join(_CATALOG_PREFIXES)}.")
    views = {v.lower() for v in shown_views}
    pending = {str(v).lower() for v in not_ready}
    for catalog, schema, name in sorted(stmt.tables):
        if name in views and catalog in ("", "memory") and schema in ("", "main"):
            continue
        shown = ".".join(part for part in (catalog, schema, name) if part)
        if not catalog and not schema and name in pending:
            raise ConnectionTableViolation(
                f"Table {shown} is listed for this connection, but its cache is not built yet, so it "
                f"cannot be queried until the files of this connection are prepared. {_tables_line(shown_views)}")
        raise ConnectionTableViolation(
            f"Table {shown} is not one of the tables of this connection. {_tables_line(shown_views)}")
    # Only fixable errors are left. A CTE name that is not plain never
    # passes, whatever it shadows.
    if stmt.describes:
        raise DuckdbQueryError(f"{_NO_DESCRIBE} {_tables_line(shown_views)}")
    if any(not _PLAIN_NAME.match(name) for name in stmt.cte_names):
        raise DuckdbQueryError(
            "A CTE on this connection needs a plain name made of letters, digits and underscores that "
            "starts with a letter or an underscore. Rename the CTE and run the query again.")
