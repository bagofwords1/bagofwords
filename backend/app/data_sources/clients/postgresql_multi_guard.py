"""The multi-database connector's query check, read by DuckDB's own parser.

Every agent query on this connector runs in one DuckDB engine that has every
tenant database attached under one login. The shared checks (`sql_guard`,
`table_scope`) read the text with sqlglot. Where sqlglot and DuckDB read the
same text differently (an escape string, an unusual quote), a query can look
harmless to the check and do something else in the engine: read a table the
agent was not given, read a tenant database directly, or replace one of the
engine's shared views.

This module closes that gap by asking the engine's parser what the query is,
through DuckDB's `json_serialize_sql`, and checking that answer:

- exactly one SELECT (anything else does not serialize at all);
- no DESCRIBE / SHOW / SUMMARIZE;
- every table it reads is one of this connection's unified views, and, when an
  agent's table selection is attached, one the agent was given;
- nothing names an attached tenant database (`__t0001`) directly;
- table functions only from a short list that reads no table, and PIVOT only
  with an explicit value list;
- no function that reads a table by name, the catalog or settings;
- no type from another catalog, in a cast or a JSON structure.

The parse runs in a separate in-memory DuckDB with nothing attached and
external access off; the query is passed as a bound parameter and never run.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field

import duckdb

from app.ai.code_execution.table_scope import _DENIED_FUNCTION, TableScopeViolation

# Table functions that produce rows from their arguments and read no table.
SAFE_TABLE_FUNCTIONS = frozenset({
    "unnest", "generate_series", "range", "json_each", "json_tree",
})

# Scalar and table functions that read the catalog, settings, a table named in
# a string, or another database through the attached connections.
DENIED_FUNCTIONS = re.compile(
    r"^(query_table|query|duckdb_.*|pragma_.*|sql_auto_complete|json_execute_serialized_sql|"
    r"json_deserialize_sql|json_serialize_sql|which_secret|glob|pg_get_viewdef|pg_get_constraintdef|"
    r"has_.*_privilege|pg_.*_is_visible|current_setting|getvariable|format_type|postgres_.*|"
    r"parquet_scan|iceberg_.*|delta_scan|sniff_csv|arrow_scan.*)$",
    re.IGNORECASE,
)

JSON_STRUCTURE_FUNCTIONS = frozenset({"json_transform", "json_transform_strict", "from_json", "from_json_strict"})

# Aliases the engine attaches tenant databases under (postgresql_multi_engine).
_TENANT_ALIAS = re.compile(r"^__t\d+$", re.IGNORECASE)

# Reference types a SELECT may draw rows from. SHOW_REF (DESCRIBE, SHOW,
# SUMMARIZE) and anything unfamiliar are refused.
_ALLOWED_REF_TYPES = frozenset({
    "BASE_TABLE", "SUBQUERY", "JOIN", "TABLE_FUNCTION", "EMPTY", "EXPRESSION_LIST", "PIVOT",
})

_parser_lock = threading.Lock()
_parser: duckdb.DuckDBPyConnection | None = None


def _parse(sql: str) -> dict:
    global _parser
    with _parser_lock:
        if _parser is None:
            con = duckdb.connect()
            con.execute("SET enable_external_access = false")
            con.execute("SET lock_configuration = true")
            _parser = con
        cur = _parser.cursor()
        try:
            return json.loads(cur.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
        finally:
            cur.close()


@dataclass
class EngineReading:
    """What DuckDB's parser says a query reads."""

    tables: list[tuple[str, ...]] = field(default_factory=list)
    table_functions: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    function_qualifiers: list[str] = field(default_factory=list)
    cte_names: set[str] = field(default_factory=set)
    ref_types: set[str] = field(default_factory=set)
    qualified_types: list[str] = field(default_factory=list)
    json_structures: list[object] = field(default_factory=list)  # str literal, or None when not a literal


def _type_name(type_info: dict) -> str:
    parts = [type_info.get("catalog") or "", type_info.get("schema") or "", type_info.get("name") or ""]
    return ".".join(p for p in parts if p)


def _walk(node, out: EngineReading) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item, out)
        return
    if not isinstance(node, dict):
        return
    kind = node.get("type") if isinstance(node.get("type"), str) else None
    if kind == "BASE_TABLE":
        out.tables.append(tuple(
            str(node.get(k) or "").lower() for k in ("catalog_name", "schema_name", "table_name") if node.get(k)
        ))
    if kind == "TABLE_FUNCTION":
        fn = node.get("function") if isinstance(node.get("function"), dict) else {}
        out.table_functions.append(str(fn.get("function_name") or "").lower())
    if kind and (kind in _ALLOWED_REF_TYPES or kind.endswith("_REF") and kind not in ("COLUMN_REF",)):
        out.ref_types.add(kind)
    if kind == "PIVOT":
        # A pivot with an explicit value list only reads its source (walked
        # below). Without one, DuckDB first runs a query and creates a type to
        # learn the values: refused.
        pivots = node.get("pivots") or []
        static = bool(pivots) and all(
            isinstance(p, dict) and p.get("entries") and not p.get("pivot_enum") for p in pivots
        )
        out.ref_types.add("PIVOT" if static else "DYNAMIC_PIVOT")
    if isinstance(node.get("function_name"), str):
        name = node["function_name"].lower()
        out.functions.append(name)
        for part in (node.get("catalog"), node.get("schema")):
            if isinstance(part, str) and part:
                out.function_qualifiers.append(part.lower())
        if name in JSON_STRUCTURE_FUNCTIONS:
            args = node.get("children") or []
            # `col.from_json(structure)` parses as a call qualified by `col`
            # with the structure as its only argument; the plain form takes it
            # second.
            structure = args[0] if len(args) == 1 else (args[1] if len(args) > 1 else None)
            value = (structure or {}).get("value") if isinstance(structure, dict) else None
            literal = value.get("value") if isinstance(value, dict) and not value.get("is_null") else None
            constant = isinstance(structure, dict) and structure.get("type") == "VALUE_CONSTANT"
            out.json_structures.append(literal if constant and isinstance(literal, str) else None)
    type_info = (node.get("cast_type") or {}).get("type_info") if isinstance(node.get("cast_type"), dict) else None
    if isinstance(type_info, dict) and type_info.get("catalog"):
        out.qualified_types.append(_type_name(type_info))
    if isinstance(node.get("cte_map"), dict):
        for entry in node["cte_map"].get("map") or []:
            if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                out.cte_names.add(entry["key"].lower())
    for value in node.values():
        _walk(value, out)


def read_query(sql: str) -> EngineReading:
    """DuckDB's own reading of `sql`. Refuses anything that is not exactly
    one SELECT statement."""
    try:
        parsed = _parse(sql)
    except duckdb.Error as exc:
        raise TableScopeViolation("Could not verify this query, so it was not run.") from exc
    if parsed.get("error"):
        raise TableScopeViolation("Run one SELECT (or WITH ... SELECT) query at a time; nothing else is allowed.")
    statements = parsed.get("statements") or []
    if len(statements) != 1:
        raise TableScopeViolation("Run one SELECT (or WITH ... SELECT) query at a time; nothing else is allowed.")
    reading = EngineReading()
    _walk(statements[0], reading)
    return reading


def _json_leaves(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _json_leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_leaves(item)


def _structure_types(structure: str) -> list[str]:
    """Catalog-qualified types named inside a JSON structure literal, each read
    by DuckDB's parser as the type of a cast."""
    try:
        leaves = list(_json_leaves(json.loads(structure, object_pairs_hook=lambda pairs: [v for _, v in pairs])))
    except ValueError:
        return ["a structure that is not JSON"]
    found = []
    for leaf in leaves:
        try:
            reading = read_query(f"SELECT CAST(NULL AS {leaf})")
        except TableScopeViolation:
            found.append(leaf)
            continue
        if reading.tables or reading.functions or reading.table_functions:
            found.append(leaf)
        found.extend(reading.qualified_types)
    return found


def check_query(sql: str, views: dict[str, list[str]], scope=None) -> list[str]:
    """Check `sql` as DuckDB reads it, and return the unified views it reads
    (names as this connection indexed them).

    `views` is every unified view the connection indexed. `scope`, when an
    agent's table selection is attached, limits that further to the views
    the agent was given.
    """
    reading = read_query(sql)

    odd = sorted(t for t in reading.ref_types if t not in _ALLOWED_REF_TYPES)
    if odd:
        if "SHOW_REF" in odd:
            raise TableScopeViolation(
                "DESCRIBE, SHOW and SUMMARIZE are not allowed: the unified tables are already described in the schema."
            )
        raise TableScopeViolation(
            f"This query uses a construct that cannot be checked here ({', '.join(odd)}). Rewrite it as a plain SELECT."
        )

    for name in reading.table_functions:
        if name not in SAFE_TABLE_FUNCTIONS:
            raise TableScopeViolation(
                f"The table function {name.upper() or '...'}() is not allowed here, because what it reads cannot be "
                "checked. Query the unified tables directly."
            )
    if any(_TENANT_ALIAS.match(q) for q in reading.function_qualifiers):
        raise TableScopeViolation(
            "Tenant databases cannot be named directly; query the unified tables (<schema>.<table>)."
        )
    for name in reading.functions:
        if DENIED_FUNCTIONS.match(name) or _DENIED_FUNCTION.match(name):
            raise TableScopeViolation(
                f"The function {name.upper()}() is not allowed here, because what it reads cannot be checked."
            )

    foreign = list(reading.qualified_types)
    for structure in reading.json_structures:
        foreign.extend(_structure_types(structure) if structure is not None else ["a JSON structure that is not a literal"])
    foreign = [t for t in foreign if t.split(".")[0].lower() != "memory"]
    if foreign:
        raise TableScopeViolation(
            f"Type(s) outside this connection's unified tables: {', '.join(dict.fromkeys(foreign))}. "
            "Cast to built-in types only."
        )

    by_key = {tuple(name.lower().split(".", 1)): name for name in views}
    active = None
    if scope is not None:
        active = {tuple(n[-2:]) for n in getattr(scope, "active", ()) if len(n) >= 2}
    used: list[str] = []
    outside: list[str] = []
    for ref in reading.tables:
        if any(_TENANT_ALIAS.match(part) for part in ref):
            raise TableScopeViolation(
                "Tenant databases cannot be named directly; query the unified tables (<schema>.<table>)."
            )
        if len(ref) == 1:
            main_view = ("main", ref[0])
            if main_view in by_key and active is not None and main_view not in active:
                outside.append(".".join(main_view))
                continue
            if ref[0] in reading.cte_names:
                continue
            outside.append(ref[0])
            continue
        if len(ref) == 3 and ref[0] != "memory":
            outside.append(".".join(ref))
            continue
        key = ref[-2:]
        if key not in by_key or (active is not None and key not in active):
            outside.append(".".join(ref))
            continue
        if by_key[key] not in used:
            used.append(by_key[key])
    if outside:
        raise TableScopeViolation(
            f"Table(s) not available on this connection: {', '.join(dict.fromkeys(outside))}. "
            "Name every table as <schema>.<table>, for example public.orders; only the agent's "
            "selected tables can be queried."
        )
    return used
