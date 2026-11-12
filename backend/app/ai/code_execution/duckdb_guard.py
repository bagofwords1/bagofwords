"""DuckDB SQL read the way DuckDB reads it.

`inspect_duckdb_sql` hands the text to DuckDB's own parser
(`json_serialize_sql`) on a private in-memory connection that has external
access off and its configuration locked. Serializing only parses: nothing is
bound, so no file, extension or address named in the text is opened. Only a
single SELECT serializes; anything else is refused.

`check_duckdb_file_query` is the rule for connections over local files
(CSV, QVD, PBIX, a Power BI Report Server report's model): one SELECT that
reads only the connection's own tables (or its own CTEs), calls no file
reader or settings function, and uses no table function but a few
generators. The client runs it on the connection it then executes on, after
that connection is locked (`app.data_sources.clients._duckdb_lockdown`).
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass

import duckdb

from app.ai.code_execution.sql_guard import ReadOnlyViolation
from app.ai.code_execution.table_scope import TableScopeViolation


@dataclass(frozen=True)
class DuckdbStatement:
    """What one SELECT reads. Names are lowercased; a table is
    (catalog, schema, name), with "" for a part the text leaves out."""

    tables: frozenset[tuple[str, str, str]]
    table_functions: frozenset[str]
    functions: frozenset[str]
    cte_names: frozenset[str]


# One connection for every call, created on first use. The sandbox runs each
# query on a thread of its own and a DuckDB connection is not safe for
# concurrent use, so creating it and every call on it hold this lock.
_LOCK = threading.Lock()
_CONNECTION: duckdb.DuckDBPyConnection | None = None

# Table references the parser emits for a plain read. Any other kind (the
# forms DESCRIBE, SHOW and SUMMARIZE become, or one a later DuckDB adds) is
# refused rather than guessed at.
_READ_TABLE_REFS = frozenset({
    "BASE_TABLE", "JOIN", "SUBQUERY", "TABLE_FUNCTION", "EXPRESSION_LIST", "EMPTY", "EMPTY_FROM", "PIVOT",
})

_ONLY_SELECT = "Only a single SELECT query is allowed on this connection."


def _connection() -> duckdb.DuckDBPyConnection:
    global _CONNECTION
    if _CONNECTION is None:
        _CONNECTION = duckdb.connect(
            database=":memory:",
            config={"enable_external_access": False, "lock_configuration": True},
        )
    return _CONNECTION


def _serialize(sql: str) -> dict:
    with _LOCK:
        raw = _connection().execute("SELECT json_serialize_sql($1)", [sql]).fetchone()[0]
    tree = json.loads(raw)
    if not isinstance(tree, dict):
        raise ValueError("unexpected serialization")
    return tree


class _Reader:
    """Collects what a serialized statement reads."""

    def __init__(self) -> None:
        self.tables: set[tuple[str, str, str]] = set()
        self.table_functions: set[str] = set()
        self.functions: set[str] = set()
        self.cte_names: set[str] = set()

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
            if kind not in _READ_TABLE_REFS:
                raise ReadOnlyViolation(_ONLY_SELECT)
            if kind == "BASE_TABLE":
                self.tables.add(tuple(str(node.get(k) or "").lower()
                                      for k in ("catalog_name", "schema_name", "table_name")))
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
        cte_map = node.get("cte_map")
        if isinstance(cte_map, dict):
            for entry in cte_map.get("map") or []:
                self.cte_names.add(str(entry.get("key", "")).lower())
        for value in node.values():
            self.walk(value)


def inspect_duckdb_sql(sql: str) -> DuckdbStatement:
    """The tables, table functions, functions and CTE names of `sql`, which
    must be exactly one SELECT. Raises ReadOnlyViolation otherwise, or when
    the text cannot be read. Opens no file and no connection."""
    if not isinstance(sql, str):
        raise ReadOnlyViolation(_ONLY_SELECT)
    try:
        tree = _serialize(sql)
    except Exception as err:
        raise ReadOnlyViolation(f"The query could not be read. {_ONLY_SELECT}") from err
    if tree.get("error"):
        if str(tree.get("error_type", "")).lower() == "parser":
            # The agent's own text, quoted back so it can fix it.
            raise ReadOnlyViolation(str(tree.get("error_message") or "The query could not be parsed."))
        raise ReadOnlyViolation(_ONLY_SELECT)
    statements = tree.get("statements")
    if not isinstance(statements, list) or len(statements) != 1:
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
    )


# Functions that read files, settings, secrets or the environment, or run
# SQL held in a string.
_DENIED_FUNCTIONS = frozenset({
    "current_setting", "getenv", "duckdb_settings", "duckdb_secrets", "duckdb_extensions",
    "query", "query_table", "glob",
})
_ALLOWED_TABLE_FUNCTIONS = frozenset({"range", "generate_series", "unnest"})
_PLAIN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _denied(function: str) -> bool:
    return function in _DENIED_FUNCTIONS or function.startswith("read_") or function.endswith("_scan")


def check_duckdb_file_query(sql: str, allowed_views: Iterable[str]) -> None:
    """Refuse `sql` unless it is one SELECT reading only `allowed_views`
    (the connection's own tables, matched case-insensitively, in the default
    schema) or CTEs it defines, with no file reader, settings read or table
    function other than range / generate_series / unnest.

    ReadOnlyViolation for the statement or a function, TableScopeViolation
    for a table."""
    stmt = inspect_duckdb_sql(sql)
    for function in sorted(stmt.functions | stmt.table_functions):
        if _denied(function):
            raise ReadOnlyViolation(
                f"{function}() is not allowed on this connection. Query its tables by name.")
    for function in sorted(stmt.table_functions - _ALLOWED_TABLE_FUNCTIONS):
        raise ReadOnlyViolation(
            f"Table function {function}() is not allowed on this connection. Query its tables by name.")
    views = {str(v).lower() for v in allowed_views}
    for catalog, schema, name in sorted(stmt.tables):
        if name in views and catalog in ("", "memory") and schema in ("", "main"):
            continue
        if not catalog and not schema and name in stmt.cte_names and _PLAIN_NAME.match(name):
            continue
        shown = ".".join(part for part in (catalog, schema, name) if part)
        raise TableScopeViolation(
            f"Table '{shown}' is not one of this connection's tables. Query only the tables it lists.")
