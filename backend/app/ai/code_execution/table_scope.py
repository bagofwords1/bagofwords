"""Query-time check that agent SQL only reads the agent's activated tables.

Turning a table off in an agent's Tables settings ("inactive") removes it from
the schema the agent is shown. Without this check that was the only effect:
the agent could still name the table in SQL and the database would answer,
because the connection's credentials can read it. This module makes the
selection hold at query time for SQL sources.

How it works:

  * `construct_clients` attaches a `TableScope` to each client (the active and
    inactive table names of that connection, from `DataSourceTable`). A client
    with no scope attached is not checked; that is the case for data sources
    that were never indexed, where there is no selection to enforce.
  * The code-execution wrapper calls `enforce_table_scope` on the final query
    text, next to the read-only guard.

Names are compared the way the engine reads them: Postgres names folded
as Postgres folds them (unquoted ones with their ASCII letters lowered,
quoted ones as written) against the names as the catalog stores them;
DuckDB names, quoted or not, with their ASCII letters lowered on both
sides, as DuckDB compares them; other dialects' names case-insensitively. A table reference is set aside
as a CTE only when a WITH entry holding a query has exactly the name as
written (for Postgres, as Postgres folds it); any other spelling is
checked as a table. No WITH entry may take the name of a disabled table:
wherever an engine reads such a name as the table rather than the entry,
the table would be read under it. Where an engine resolves an entry's body
where the entry is used (ClickHouse, MySQL) or the dialect is not known,
WITH entries that refer to each other in a loop are refused too, and on
ClickHouse so is a lambda that is a WITH entry or has an alias. On
ClickHouse, the set of an IN is read only in the shapes `_set_item_tables`
lists (never under a name an alias of the query also has), and a SETTINGS
clause only with the settings `_CLICKHOUSE_QUERY_SETTINGS` lists; anything
else is refused. DuckDB statements are also read with DuckDB's own parser
(`_duckdb_tables`): its reads list decides what a statement is, and the
tables DuckDB reads are checked as well as the ones sqlglot finds.

The check fails closed. If sqlglot cannot parse the query, reads it
differently from the engine (`sql_guard.check_engine_reading`), or the query reads
through something whose target cannot be named (`SHOW`, `EXPLAIN`,
`IDENTIFIER('...')`, `RESULT_SCAN(...)`, an unknown table function, functions
that run SQL held in a string such as Postgres `query_to_xml`), the query is
refused rather than run unchecked.

Scope: SQL clients whose dialect sqlglot knows (see `sql_guard.CLIENT_DIALECTS`).
Non-SQL clients (Mongo, Splunk, Salesforce, API and MCP connectors) are not
checked here. This is a second layer: the hard boundary is still the grants of
the database login behind the connection.
"""

from __future__ import annotations

import logging
import re
import string
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.ai.code_execution.sql_guard import (
    DENIED_FUNCTIONS,
    ReadOnlyViolation,
    check_engine_reading,
    dialect_for_client,
    duckdb_read,
)

logger = logging.getLogger(__name__)

TABLE_SCOPE_ATTR = "_bow_table_scope"

Name = tuple[str, ...]  # lowercased name parts, e.g. ("gold", "orders")


class TableScopeViolation(ValueError):
    """The query reads a table outside the agent's activated set, or its
    tables cannot be verified."""


def _split_name(name: str, lower: bool = True) -> Name:
    parts = [p.strip().strip('"`[]') for p in str(name).split(".")]
    return tuple(p.lower() if lower else p for p in parts if p)


# Postgres folds unquoted names to lower case, ASCII letters only, and
# keeps quoted names as written.
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
# Dialects whose names are compared exactly, once folded as their engine
# folds them, against the names as the catalog stores them.
_ENGINE_FOLDED = frozenset({"postgres"})
# Dialects whose engine reads the names in a WITH entry's body where the
# entry is defined (Postgres 15 was measured). Every other dialect, an
# unknown one and none included, is treated as resolving them where the
# entry is used, as ClickHouse and MySQL 8.4 do (sqlglot's "mysql" also
# serves MariaDB).
_WITH_RESOLVED_WHERE_DEFINED = frozenset({
    "postgres", "redshift", "tsql", "fabric", "snowflake", "bigquery", "duckdb", "sqlite", "oracle",
    "trino", "presto", "athena", "spark", "databricks", "teradata", "druid",
})


def _qualify(name: Name, default_database: str | None) -> Name:
    """A `schema.table` name read the way the database reads it: in the
    session's default database. Other lengths are left as they are."""
    if default_database and len(name) == 2:
        return (default_database, *name)
    return name


@dataclass(frozen=True)
class TableScope:
    active: frozenset[Name] = field(default_factory=frozenset)
    inactive: frozenset[Name] = field(default_factory=frozenset)
    # The connection's default database, for clients that name tables
    # `schema.table` in it (and `database.schema.table` elsewhere). With it set,
    # every two-part name, stored or written, is compared as
    # `default.schema.table`, so a table in another database can no longer
    # pass for an enabled one that merely shares its schema and table name.
    default_database: str | None = None
    # The same selection compared the old way, without database qualification.
    # Set for connections that predate the database check: a query that only
    # the old comparison allows is logged and still run, so existing saved
    # queries keep working while the log shows whether anyone relies on it.
    legacy: TableScope | None = None
    # The same selection with each name as the catalog stores it (quotes
    # stripped, case kept), for dialects whose names are compared the way
    # their engine folds them (`_ENGINE_FOLDED`).
    exact: TableScope | None = None
    # The same selection with each name as the catalog stores it, its ASCII
    # letters lowered and nothing else (`_ASCII_LOWER`), the way DuckDB
    # compares names; for DuckDB, whose names are read that way. A DuckDB
    # query against a scope without it is refused as unverified.
    ascii_folded: TableScope | None = None

    @classmethod
    def from_names(
        cls,
        active: Iterable[str],
        inactive: Iterable[str],
        *,
        default_database: str | None = None,
        enforce_databases: bool = True,
        lower: bool = True,
    ) -> TableScope:
        active, inactive = list(active), list(inactive)
        db = default_database.strip().strip('"`[]') if isinstance(default_database, str) else ""
        db = (db.lower() if lower else db) or None
        act = frozenset(_qualify(n, db) for n in (_split_name(x, lower) for x in active) if n)
        # A name activated anywhere wins over an inactive duplicate.
        ina = frozenset(_qualify(n, db) for n in (_split_name(x, lower) for x in inactive) if n) - act
        legacy = None
        if db and not enforce_databases:
            legacy = cls.from_names(active, inactive, lower=lower)
        exact = ascii_folded = None
        if lower:
            exact = cls.from_names(active, inactive, default_database=default_database,
                                   enforce_databases=enforce_databases, lower=False)
            ascii_folded = cls.from_names(
                [str(x).translate(_ASCII_LOWER) for x in active], [str(x).translate(_ASCII_LOWER) for x in inactive],
                default_database=(default_database.translate(_ASCII_LOWER)
                                  if isinstance(default_database, str) else None),
                enforce_databases=enforce_databases, lower=False)
        return cls(active=act, inactive=ina, default_database=db, legacy=legacy, exact=exact,
                   ascii_folded=ascii_folded)


def _suffix_match(ref: Name, known: Name) -> bool:
    """True when the shorter name is a trailing part of the longer one.

    `lakehouse.gold.x` matches the catalog entry `gold.x` (catalog names omit
    the database), and an unqualified `x` matches `gold.x`.
    """
    n = min(len(ref), len(known))
    return n > 0 and ref[-n:] == known[-n:]


def is_allowed(ref: Name, scope: TableScope) -> bool:
    if not any(_suffix_match(ref, a) for a in scope.active):
        return False
    # An under-qualified reference (`x`, or `gold.x` against a catalog that
    # stores `db.gold.x`) is ambiguous when an inactive table also ends with
    # it: the database decides which one it reads, not us. Refuse.
    return not any(len(ref) < len(i) and _suffix_match(ref, i) for i in scope.inactive)


# Table functions that produce rows from their arguments and read no table.
_SAFE_TABLE_FUNCTIONS = frozenset({
    "generate_series", "unnest", "flatten", "generator", "split_to_table",
    "strtok_split_to_table", "explode", "posexplode", "explode_outer",
    "posexplode_outer", "inline", "range", "regexp_split_to_table",
    "string_to_table", "json_array_elements", "json_array_elements_text",
    "jsonb_array_elements", "jsonb_array_elements_text", "json_each",
    "json_each_text", "jsonb_each", "jsonb_each_text", "json_to_recordset",
    "jsonb_to_recordset", "json_populate_recordset", "jsonb_populate_recordset",
    "json_table", "openjson", "string_split", "generate_array",
    "generate_date_array", "generate_timestamp_array",
})

# Functions that read a table named in a string, run SQL held in a string, or
# reach outside the connection. Refused wherever they appear.
_DENIED_FUNCTION = re.compile(
    r"^(identifier|result_scan|table|infer_schema|get_ddl|ts_stat|ts_rewrite|"
    r"query_to_xml.*|table_to_xml.*|schema_to_xml.*|database_to_xml.*|cursor_to_xml.*|"
    r"dblink.*|pg_read_.*|pg_ls_.*|lo_.*|"
    r"system\$.*|external_query|"
    r"read_(csv|json|parquet|text|blob|ndjson|xlsx).*|sqlite_scan|postgres_scan|mysql_scan|"
    r"openrowset|openquery|opendatasource|"
    r"url|file|s3|gcs|hdfs|remote|remotesecure|mysql|postgresql|jdbc|odbc|"
    r"joinget.*|dictget.*|dicthas|dictisin|hascolumnintable)$",
    re.IGNORECASE,
)

# ClickHouse's IN operators written as functions (globalIn, notNullIn,
# inIgnoreSet, ...): the second argument is the set, as on the right of IN.
_IN_FUNCTION = re.compile(r"(global)?(not)?(null)?in(ignoreset)?", re.IGNORECASE)

# ClickHouse query settings an agent query may change (SETTINGS name =
# value), each with a plain value. Every other setting is refused: some
# hold SQL text, which table scope cannot check, and a list of those would
# have to keep up with every server release.
_CLICKHOUSE_QUERY_SETTINGS = frozenset({
    "max_execution_time", "max_threads", "max_block_size", "max_memory_usage",
    "max_rows_to_read", "max_bytes_to_read", "read_overflow_mode",
    "max_result_rows", "max_result_bytes", "result_overflow_mode",
    "timeout_overflow_mode", "join_use_nulls", "session_timezone",
})


def _unverifiable_function(name: str) -> TableScopeViolation:
    return TableScopeViolation(
        f"The function {name.upper()}() is not allowed in agent queries, because "
        "the table it reads cannot be checked. Query the enabled tables directly."
    )


def _func_name(node) -> str:
    from sqlglot import exp

    if isinstance(node, exp.Anonymous):
        return str(node.name or "")
    try:
        return str(node.sql_name() or "")
    except Exception:
        return ""


def _name_part(ident, dialect: str | None) -> str:
    """A name (an Identifier) as `dialect`'s engine compares it: for
    Postgres, a quoted name as written and an unquoted one with its ASCII
    letters lowered; elsewhere lowered."""
    name = str(ident.name or "")
    if dialect in _ENGINE_FOLDED:
        return name if ident.quoted else name.translate(_ASCII_LOWER)
    return name.lower()


def _table_ref(table, dialect: str | None) -> Name:
    from sqlglot import exp

    if dialect == "duckdb":
        # DuckDB folds every name, quoted or not, ASCII letters only.
        return tuple(p.translate(_ASCII_LOWER) for p in (table.catalog, table.db, table.name) if p)
    if dialect not in _ENGINE_FOLDED:
        return tuple(p.lower() for p in (table.catalog, table.db, table.name) if p)
    parts = []
    for key in ("catalog", "db", "this"):
        part = table.args.get(key)
        name = _name_part(part, dialect) if isinstance(part, exp.Identifier) else table.text(key).lower()
        if name:
            parts.append(name)
    return tuple(parts)


def _resolves_to_cte(table, dialect: str | None = None) -> bool:
    """True when an unqualified table reference names a CTE where it
    stands: at least one WITH entry visible there has the name as written
    (`_same_name`), and every such entry holds a query. Anything else is
    checked as a table: a name written in another case or quoted otherwise
    (engines differ in how they compare the two), and a WITH entry that
    holds a value rather than a query (ClickHouse's `WITH 1 AS name`, or
    `WITH (SELECT ...) AS name`), which does not stand for a table.

    Which entries are visible where the reference stands is
    `_visible_with_entries` (inside an entry's own body, the entries
    defined before it).
    """
    entries = _visible_with_entries(table, lambda ident: _same_name(ident, table.this, dialect), later=False)
    return bool(entries) and all(_holds_query(entry) for entry in entries)


def _visible_with_entries(node, named, *, later: bool) -> list:
    """The WITH entries visible where `node` stands whose name (an
    Identifier) `named` accepts. Both readings of a name as a WITH entry
    use it: a table reference (`_resolves_to_cte`) and the set of a
    ClickHouse IN (`_with_entries`).

      * A WITH is visible only inside the query it belongs to and the
        queries nested in it.
      * Inside an entry's own body the entry is not visible: the engine
        reads its name there as the table of that name (ClickHouse even in
        a WITH RECURSIVE entry). The one exception is the recursive part of
        a WITH RECURSIVE entry whose body is a UNION: inside the UNION's
        operands after the first, the name is the entry.
      * Inside an entry's own body, the entries of that WITH defined before
        it are visible, and with `later` the ones after it too (ClickHouse
        reads them as the set of an IN).
    """
    from sqlglot import exp

    found = []
    path: set[int] = set()  # `node` and the ancestors passed so far
    child, inside = node, None
    parent = node.parent
    while parent is not None:
        path.add(id(child))
        if isinstance(parent, exp.CTE):
            inside = parent
        w = parent.args.get("with")
        if isinstance(w, exp.With):
            entries = list(w.expressions)
            i = next((k for k, e in enumerate(entries) if e is inside), None) if child is w else None
            if i is not None:
                visible = entries[:i] + (entries[i + 1:] if later else [])
                if w.args.get("recursive") and _in_recursive_part(inside, path):
                    visible.append(inside)
            else:
                visible = entries
            found += [c for c in visible if (ident := _cte_ident(c)) is not None and named(ident)]
        child, parent = parent, parent.parent
    return found


def _in_recursive_part(entry, path: set[int]) -> bool:
    """Whether a node whose ancestors are `path` (ids) stands in the
    recursive part of `entry`: its body is a UNION and the node is inside
    one of the UNION's operands after the first."""
    from sqlglot import exp

    body = entry.this
    while isinstance(body, exp.Union):
        if id(body.expression) in path:
            return True
        body = body.this
    return False


def _cte_ident(cte):
    """The name of a WITH entry (an Identifier), or None."""
    from sqlglot import exp

    alias = cte.args.get("alias")
    ident = alias.this if isinstance(alias, exp.TableAlias) else None
    return ident if isinstance(ident, exp.Identifier) else None


def _same_name(ident, written, dialect: str | None) -> bool:
    """Whether a WITH entry's name `ident` is the name `written`: for
    Postgres, as Postgres folds both (`_name_part`); elsewhere written
    alike, letter for letter, and quoted alike."""
    from sqlglot import exp

    if not isinstance(ident, exp.Identifier) or not isinstance(written, exp.Identifier):
        return False
    if dialect in _ENGINE_FOLDED:
        return _name_part(ident, dialect) == _name_part(written, dialect)
    return ident.name == written.name and bool(ident.quoted) == bool(written.quoted)


def _holds_query(cte) -> bool:
    """Whether a WITH entry is a query its name stands for as a table."""
    from sqlglot import exp

    return not cte.args.get("scalar") and isinstance(cte.this, (exp.Query, exp.Values))


def tables_read(statement, dialect: str | None = None) -> list[Name]:
    """The tables `statement` reads by name, each once: every
    table reference that is an identifier (not a table function), except a
    one-part name that refers to a CTE visible where it stands, and on
    ClickHouse every table read as the set of an IN (`_in_set_tables`).
    Name parts lowered, or for Postgres and DuckDB (`dialect`) folded as
    their engine folds them. Raises TableScopeViolation for WITH entries that refer to
    each other (`_check_with_loops`, over the whole statement `statement`
    belongs to) and for a ClickHouse IN set outside the shapes it reads."""
    from sqlglot import exp

    _check_with_loops(statement.root(), dialect)
    out: dict[Name, None] = {}  # insertion-ordered, each name once
    for t in statement.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            continue
        ref = _table_ref(t, dialect)
        if ref in out or (len(ref) == 1 and _resolves_to_cte(t, dialect)):
            continue
        out[ref] = None
    if dialect == "clickhouse":
        out.update(dict.fromkeys(_in_set_tables(statement)))
    return list(out)


def _with_resolved_where_used(dialect: str | None) -> bool:
    """Whether `dialect`'s engine may read the names in a WITH entry's body
    where the entry is used (`_WITH_RESOLVED_WHERE_DEFINED`)."""
    return dialect not in _WITH_RESOLVED_WHERE_DEFINED


def _check_with_loops(statement, dialect: str | None = None) -> None:
    """Refuse, where an engine resolves an entry's body where the entry is
    used (`_with_resolved_where_used`), WITH entries of `statement`, at any
    level, that refer to each other in a loop: an entry whose body (nested
    queries and WITHs included) names, compared case-insensitively, another
    entry that leads back to it. An entry naming itself is not a loop:
    inside its own body its name is the table of that name, or the entry in
    the recursive part of a WITH RECURSIVE (`_visible_with_entries`), and
    is checked as such.

    While such an engine reads an entry, it reads that entry's name as the
    table of that name, so which entry or table a name stands for depends
    on the way it was reached; within a loop the visibility rules of
    `_visible_with_entries` cannot say which. Every name that may refer to
    an entry counts (a one-part table name, a column, a bare name), so a
    column that only shares another entry's name also makes a loop. The
    name rule (`_check_names_of_disabled_tables`) already refuses every
    loop that reaches a disabled table by name; this check is a second
    line."""
    from sqlglot import exp

    if not _with_resolved_where_used(dialect):
        return
    entries = list(statement.find_all(exp.CTE))
    by_name: dict[str, list[int]] = {}
    for k, entry in enumerate(entries):
        ident = _cte_ident(entry)
        if ident is not None:
            by_name.setdefault(str(ident.name).lower(), []).append(k)
    edges: list[set[int]] = []
    for k, entry in enumerate(entries):
        named: set[int] = set()
        body = entry.this
        for ident in (body.find_all(exp.Identifier) if body is not None else ()):
            targets = by_name.get(str(ident.name).lower())
            if not targets or _defines_name(ident):
                continue
            named.update(j for j in targets if j != k)
        edges.append(named)
    if _has_loop(edges):
        raise TableScopeViolation(
            "WITH entries that refer to each other are not allowed in agent queries, because the "
            "table each name reads cannot be checked. Give each WITH entry a name the entries it "
            "uses do not use, or write the query without the loop."
        )


def _check_names_of_disabled_tables(statement, scope: TableScope) -> None:
    """Refuse a WITH entry (a query, a value or a lambda, at any level)
    whose name is the name of a table the scope disables, or a trailing part
    of it: both read by `_split_name` (so a dot, brackets, quotes and spaces
    around a part count alike on both sides) and compared case-insensitively.
    An engine that reads such a name as the table rather than the entry (in
    the entry's own body, which ClickHouse and MySQL resolve where the entry
    is used; through a lambda called by name) would read the disabled table
    under it, whatever path led there. With the rule, a name an engine falls
    back from is a table that is enabled, or not a disabled table of the
    catalog. An alias needs no such rule: table scope never sets its name
    aside, so a table read under it is checked as that table."""
    from sqlglot import exp

    disabled = frozenset(
        tuple(p.casefold() for p in ref[k:])
        for s in (scope, scope.exact) if s is not None for ref in s.inactive for k in range(len(ref)))
    if not disabled:
        return
    for cte in statement.find_all(exp.CTE):
        ident = _cte_ident(cte)
        if ident is None:
            continue
        name = str(ident.name)
        if tuple(p.casefold() for p in _split_name(name, lower=False)) in disabled:
            raise TableScopeViolation(
                f"{_display((name,))} is not enabled for this agent, and a WITH entry may not take its name, "
                "because the source may read the table under it. Rename the WITH entry; the agent's enabled "
                "tables can be read under their own names."
            )


def _check_clickhouse_with_lambdas(statement) -> None:
    """Refuse a ClickHouse lambda (`x -> expr`) anywhere but as the argument
    of a function: a WITH entry that is a lambda is called by its name,
    which can be a built-in's, and its body is read where it is called.
    sqlglot reads `->` outside a function's arguments as a JSON operator,
    which ClickHouse does not have. The function form, `lambda(tuple(x),
    expr)`, is refused as a WITH value and under an alias anywhere (called
    by that name); passed to a function as it is, it still reads."""
    from sqlglot import exp

    lambda_entry = False
    for cte in statement.find_all(exp.CTE):
        value = cte.this
        while isinstance(value, exp.Paren):
            value = value.this
        lambda_entry = lambda_entry or isinstance(value, exp.Lambda)
    for fn in statement.find_all(exp.Anonymous):
        if str(fn.name).lower() != "lambda":
            continue
        holder = fn.parent
        while isinstance(holder, exp.Paren):
            holder = holder.parent
        lambda_entry = lambda_entry or isinstance(holder, (exp.CTE, exp.Alias))
    if lambda_entry or any(True for _ in statement.find_all(exp.JSONExtract)):
        raise TableScopeViolation(
            "A lambda (x -> expression) is allowed in agent queries only as the argument of a function such "
            "as arrayMap, without a name: a lambda that is a WITH entry or has an alias is read where it is "
            "called, which table scope cannot check. Write the expression where it is used."
        )


def _defines_name(ident) -> bool:
    """Whether an Identifier defines a name rather than refers to one: an
    alias (of a column, a table or a WITH entry, with its column names), a
    lambda's parameter, or a part of a table name written with its schema
    or database (that table, never a WITH entry)."""
    from sqlglot import exp

    parent = ident.parent
    if isinstance(parent, exp.TableAlias):
        return True
    if isinstance(parent, exp.Alias) and parent.args.get("alias") is ident:
        return True
    if isinstance(parent, exp.Lambda) and ident.arg_key == "expressions":
        return True
    return isinstance(parent, exp.Table) and bool(parent.args.get("db") or parent.args.get("catalog"))


def _has_loop(edges: list[set[int]]) -> bool:
    """Whether the graph `edges` (node k -> the nodes in edges[k]) has a
    cycle, a node pointing at itself included. Iterative, linear."""
    state = [0] * len(edges)  # 0 unseen, 1 on the current path, 2 done
    for start in range(len(edges)):
        if state[start]:
            continue
        state[start] = 1
        stack = [(start, iter(edges[start]))]
        while stack:
            node, rest = stack[-1]
            nxt = next(rest, None)
            if nxt is None:
                state[node] = 2
                stack.pop()
            elif state[nxt] == 1:
                return True
            elif state[nxt] == 0:
                state[nxt] = 1
                stack.append((nxt, iter(edges[nxt])))
    return False


def reads_rows(statement, dialect: str | None = None) -> bool:
    """Whether `statement` may read rows of a table: it names a table or a
    table function, calls a function that reads a table, a dictionary or a
    file by name (`_DENIED_FUNCTION`), or (ClickHouse) uses a table, or a
    set table scope cannot read, as the set of an IN. Where an engine
    resolves a WITH entry's body where it is used
    (`_with_resolved_where_used`), any WITH entry in the whole statement
    `statement` belongs to counts as reading rows: this check runs without
    a scope, so the name rule cannot tell a disabled table's name there."""
    from sqlglot import exp

    if _with_resolved_where_used(dialect) and statement.root().find(exp.CTE) is not None:
        return True
    if any(True for _ in statement.find_all(exp.Table)):
        return True
    for fn in statement.find_all(exp.Func):
        name = _func_name(fn)
        if name and _DENIED_FUNCTION.match(name):
            return True
    try:
        return bool(tables_read(statement, dialect))
    except TableScopeViolation:
        return True


def _unread_in_set() -> TableScopeViolation:
    return TableScopeViolation(
        "Could not verify which table this IN reads, so the query was not run. Write the set "
        "as a list of values, a subquery over the agent's enabled tables, or an enabled table's name."
    )


def _in_set_tables(statement) -> list[Name]:
    """The tables ClickHouse reads as the set of an IN (GLOBAL IN, NOT IN)
    or of an IN function (globalIn(x, set), notNullIn, ...), where sqlglot
    reads a column. Only these set shapes are read (`_set_item_tables`);
    any other, a set sqlglot left empty, and an IN function with other
    than two arguments are refused."""
    from sqlglot import exp

    out: list[Name] = []
    followed: dict = {}  # shared by every set of the statement (`_set_item_tables`)
    # Every alias name of the whole statement (a WITH entry's name is not an
    # alias here), compared case-insensitively.
    aliases = frozenset(str(a.alias).casefold() for a in statement.root().find_all(exp.Alias))
    for node in statement.find_all(exp.In):
        query, field, items = node.args.get("query"), node.args.get("field"), list(node.expressions)
        if node.args.get("unnest") is not None or [query is not None, field is not None, bool(items)].count(True) != 1:
            raise _unread_in_set()
        if query is not None:
            if not isinstance(query, exp.Query):
                raise _unread_in_set()
            continue  # its tables are read where it stands
        for item in [field] if field is not None else items:
            out += _set_item_tables(item, followed, aliases)
    for node in statement.find_all(exp.Func):
        if _IN_FUNCTION.fullmatch(_func_name(node)):
            if not isinstance(node, exp.Anonymous) or len(node.expressions) != 2:
                raise _unread_in_set()
            out += _set_item_tables(node.expressions[1], followed, aliases)
    return out


# The most WITH entries `_set_item_tables` follows for one statement's IN
# sets. More is refused: it keeps the check's time and depth bounded.
_MAX_SET_FOLLOWS = 64
_FOLLOWING = object()


def _set_item_tables(node, followed: dict, aliases: frozenset[str]) -> list[Name]:
    """The tables ClickHouse reads for one item of an IN set, with any
    parentheses and alias around it unwrapped:

      * a value (a literal, NULL, TRUE/FALSE, a negative number) reads none;
        a tuple or an array reads what its items read;
      * a query reads none here (its tables are read where it stands);
      * a name: a one-part name with WITH entries of that exact name
        (`_with_entries`) reads what every one of them holds, read as an
        item in turn; any other name is the table of that name.

    Each entry is followed once per statement: `followed` holds what each
    entry (by id) reads, or `_FOLLOWING` while it is being followed.
    Anything else, WITH entries that name each other in a loop, and more
    than `_MAX_SET_FOLLOWS` entries followed are refused. So is a name any
    part of which is, compared case-insensitively, one of `aliases` (the
    statement's alias names): ClickHouse may read the alias's value there
    instead of the table of that name."""
    from sqlglot import exp

    while isinstance(node, (exp.Paren, exp.Alias)):
        node = node.this
    if _is_value(node) or isinstance(node, exp.Query):
        return []
    if isinstance(node, (exp.Tuple, exp.Array)):
        return _distinct(ref for item in node.expressions for ref in _set_item_tables(item, followed, aliases))
    if isinstance(node, exp.Identifier):
        parts = [node]
    elif isinstance(node, exp.Column) and all(isinstance(p, exp.Identifier) for p in node.parts):
        parts = node.parts
    else:
        raise _unread_in_set()
    if any(str(p.name).casefold() in aliases for p in parts):
        raise TableScopeViolation(
            "Could not verify which table this IN reads, because its set is named like an alias in the "
            "query, which the source may read instead of the table. Rename the alias."
        )
    entries = _with_entries(node, parts[0].name) if len(parts) == 1 else []
    if not entries:
        return [tuple(str(p.name).lower() for p in parts)]
    out: list[Name] = []
    for entry in entries:
        key = id(entry)
        if key not in followed:
            if len(followed) >= _MAX_SET_FOLLOWS:
                raise _unread_in_set()
            followed[key] = _FOLLOWING
            followed[key] = _set_item_tables(entry.this, followed, aliases)
        if followed[key] is _FOLLOWING:
            raise _unread_in_set()
        out += followed[key]
    return _distinct(out)


def _distinct(refs) -> list[Name]:
    """`refs` in order, each once."""
    return list(dict.fromkeys(refs))


def _with_entries(node, name: str) -> list:
    """The WITH entries visible where `node` stands (`_visible_with_entries`,
    later entries of the same WITH included) named `name`, letter for
    letter (ClickHouse compares names case by case and reads a quoted name
    as the same name)."""
    return _visible_with_entries(node, lambda ident: ident.name == name, later=True)


def _is_value(node) -> bool:
    from sqlglot import exp

    if isinstance(node, exp.Neg):
        node = node.this
        return isinstance(node, exp.Literal) and not node.is_string
    return isinstance(node, (exp.Literal, exp.Null, exp.Boolean))


def _check_clickhouse_settings(statement) -> None:
    """Refuse a ClickHouse SETTINGS clause, anywhere in `statement`, that
    sets anything but one of `_CLICKHOUSE_QUERY_SETTINGS` to a plain
    value."""
    from sqlglot import exp

    for node in statement.walk():
        settings = node.args.get("settings")
        if settings is None:
            continue
        for setting in settings if isinstance(settings, list) else [settings]:
            name = setting.this if isinstance(setting, exp.EQ) else None
            known = (isinstance(name, exp.Column) and isinstance(name.this, exp.Identifier) and not name.table
                     and name.name in _CLICKHOUSE_QUERY_SETTINGS and _is_value(setting.expression))
            if not known:
                shown = name.name if isinstance(name, exp.Column) else str(setting.sql(dialect="clickhouse"))[:60]
                raise TableScopeViolation(
                    f"The query setting {shown} is not allowed in agent queries, because table scope cannot "
                    f"check what it reads. Allowed settings: {', '.join(sorted(_CLICKHOUSE_QUERY_SETTINGS))}."
                )


def _display(ref: Name) -> str:
    return ".".join(p.upper() for p in ref)


_UNVERIFIED = "Could not verify which tables this query reads, so it was not run."


def _not_a_read_of_enabled_tables(verb: str) -> TableScopeViolation:
    return TableScopeViolation(
        f"'{verb or 'This'}' statements are not allowed: this agent can only "
        "read its enabled tables, which are already described in its schema."
    )


def _duckdb_tables(sql: str) -> tuple[str | None, list[Name]]:
    """A DuckDB statement as DuckDB reads it: (the SELECT left for the
    sqlglot checks, or None when there is none; the tables it reads).

    A listing read (SHOW, a listing PRAGMA) and EXPLAIN are
    refused; DESCRIBE / DESC / PRAGMA table_info of a name read that name;
    a SELECT, or the one DESCRIBE describes, reads the tables DuckDB's
    parser finds in it (its own CTEs left out), and may call no function
    whose table cannot be checked."""
    from app.ai.code_execution.duckdb_guard import inspect_duckdb_sql

    try:
        read = duckdb_read(sql)
    except ReadOnlyViolation as e:
        raise TableScopeViolation(f"{_UNVERIFIED} {e}") from e
    if read is not None and not read.scoped:
        raise _not_a_read_of_enabled_tables(read.verb)
    if read is not None and read.name is not None:
        return None, [read.name]
    select = read.select if read is not None else sql
    try:
        stmt = inspect_duckdb_sql(select)
    except Exception as e:  # any failure: the tables cannot be verified
        raise TableScopeViolation(
            f"{_UNVERIFIED} Rewrite it as a plain SELECT over the agent's enabled tables."
        ) from e
    if stmt.describes:
        raise _not_a_read_of_enabled_tables("DESCRIBE, SHOW and SUMMARIZE inside a query")
    for function in sorted(stmt.functions | stmt.table_functions):
        if _DENIED_FUNCTION.match(function) or DENIED_FUNCTIONS["duckdb"].fullmatch(function):
            raise _unverifiable_function(function)
    for function in sorted(stmt.table_functions - _SAFE_TABLE_FUNCTIONS):
        raise _unverifiable_function(function)
    return select, [tuple(part for part in table if part) for table in sorted(stmt.tables)]


def _refuse_denied(denied: list[str]) -> None:
    if denied:
        raise TableScopeViolation(
            f"Table access denied: {', '.join(denied)} "
            f"{'is' if len(denied) == 1 else 'are'} not enabled for this agent. "
            "Only the agent's selected tables can be queried; an admin can enable "
            "more in the agent's Tables settings. Do not retry with other syntax. "
            "Tell the user the table is not available to this agent."
        )


def _collect_denied(refs, scope: TableScope, dialect: str, denied: list[str],
                    exact_catalogs: tuple[str, ...] | None = None) -> None:
    """Add to `denied` each of `refs` the scope does not allow. With
    `exact_catalogs`, a table must be `<db>.<table>`, optionally behind one
    of those catalogs, and equal an active name part for part (no suffix
    matching, no legacy comparison)."""
    compared = scope
    if dialect in _ENGINE_FOLDED and scope.exact is not None:
        compared = scope.exact
    elif dialect == "duckdb":
        if scope.ascii_folded is None:
            raise TableScopeViolation(f"{_UNVERIFIED} Rewrite it as a plain SELECT over the agent's enabled tables.")
        compared = scope.ascii_folded
    for ref in refs:
        if exact_catalogs is not None:
            if not (len(ref) in (2, 3) and (len(ref) == 2 or ref[0] in exact_catalogs)
                    and ref[-2:] in compared.active):
                shown = _display(ref)
                if shown not in denied:
                    denied.append(shown)
            continue
        if not is_allowed(_qualify(ref, compared.default_database), compared):
            if compared.legacy is not None and is_allowed(ref, compared.legacy):
                logger.warning(
                    "table_scope: allowed %s only because it matches an enabled table "
                    "outside the connection's default database %s; this will be refused "
                    "once database checks are enforced",
                    _display(ref), (compared.default_database or "").upper(),
                )
                continue
            shown = _display(ref)
            if shown not in denied:
                denied.append(shown)


def check_table_scope(
    sql: str, dialect: str, scope: TableScope, exact_catalogs: tuple[str, ...] | None = None,
) -> None:
    """Raise TableScopeViolation unless every table `sql` reads is active.

    With `exact_catalogs`, a table must be `<db>.<table>`, optionally behind one of
    those catalogs, and equal an active name part for part (no suffix matching).
    """
    from app.ai.code_execution.sql_parser import parse_guard_sql
    from sqlglot import exp

    try:
        check_engine_reading(sql, dialect)
    except ReadOnlyViolation as e:
        raise TableScopeViolation(f"{_UNVERIFIED} {e}") from e
    denied: list[str] = []
    if dialect == "duckdb":
        select, refs = _duckdb_tables(sql)
        _collect_denied(refs, scope, dialect, denied, exact_catalogs)
        if select is None:
            _refuse_denied(denied)
            return
        sql = select
    try:
        statements = [s for s in parse_guard_sql(sql, dialect)
                      if s is not None and not isinstance(s, exp.Semicolon)]
    except Exception as e:  # any parser failure: the tables cannot be verified
        logger.info("table_scope: %s parser could not read query: %s", dialect, str(e)[:200])
        raise TableScopeViolation(
            f"{_UNVERIFIED} Rewrite it as a plain SELECT over the agent's enabled tables."
        ) from e

    for stmt in statements:
        if isinstance(stmt, exp.Use):
            continue  # the read-only guard only lets USE WAREHOUSE through
        if isinstance(stmt, exp.Command) or type(stmt).__name__ == "Show":
            verb = (str(stmt.this or "") if isinstance(stmt, exp.Command) else "SHOW").split(" ")[0].upper()
            raise _not_a_read_of_enabled_tables(verb)

        _check_names_of_disabled_tables(stmt, scope)
        if dialect == "clickhouse":
            _check_clickhouse_with_lambdas(stmt)
        for fn in stmt.find_all(exp.Func):
            name = _func_name(fn)
            if name and _DENIED_FUNCTION.match(name):
                raise _unverifiable_function(name)
        if dialect == "clickhouse":
            _check_clickhouse_settings(stmt)
        # A denied name as a column or a field: the read-only guard's list,
        # which holds every Postgres entry above that takes one argument
        # (the large-object ones by exact name, so a column such as lo_price
        # still reads). DuckDB's calls were read by its own parser above,
        # which tells a call from a column.
        field_denied = DENIED_FUNCTIONS.get(dialect) if dialect != "duckdb" else None
        if field_denied is not None:
            for node in stmt.find_all(exp.Column, exp.Dot):
                if field_denied.fullmatch(node.name.lower()):
                    raise _unverifiable_function(node.name)

        for tfr in stmt.find_all(exp.TableFromRows):
            inner = tfr.this
            name = _func_name(inner).lower() if inner is not None else ""
            if isinstance(inner, exp.Anonymous) and name not in _SAFE_TABLE_FUNCTIONS:
                raise TableScopeViolation(
                    f"TABLE({name.upper() or '...'}(...)) is not allowed in agent queries, "
                    "because the table it reads cannot be checked."
                )

        _collect_denied(tables_read(stmt, dialect), scope, dialect, denied, exact_catalogs)

        for t in stmt.find_all(exp.Table):
            target = t.this
            if isinstance(target, exp.Identifier):
                continue
            if type(target).__name__ == "SemanticView":
                continue  # its view name is a separate Table node, checked above
            name = _func_name(target).lower() if isinstance(target, exp.Func) else ""
            if isinstance(target, exp.Func) and not isinstance(target, exp.Anonymous):
                continue  # typed built-ins sqlglot knows (GENERATE_SERIES, ...)
            if name in _SAFE_TABLE_FUNCTIONS:
                continue
            raise TableScopeViolation(
                f"'{t.sql(dialect=dialect)[:80]}' is not allowed as a query source, because "
                "the table it reads cannot be checked."
            )

    _refuse_denied(denied)


def table_refs(sql: str, dialect: str) -> list[Name]:
    """Every table `sql` reads, as lowercased name parts, CTE names left out
    (`tables_read`: the same CTE visibility and WITH-loop rules as the table
    scope check).

    Unreadable SQL and SHOW/DESCRIBE/USE raise, never `[]`.
    """
    from app.ai.code_execution.sql_parser import parse_guard_sql
    from sqlglot import exp
    from sqlglot.errors import SqlglotError

    try:
        statements = [s for s in parse_guard_sql(sql, dialect)
                      if s is not None and not isinstance(s, exp.Semicolon)]
    except SqlglotError as e:
        raise TableScopeViolation(
            "Could not verify which tables this query reads, so it was not run. "
            "Rewrite it as a plain SELECT over <database>.<table> names."
        ) from e

    refs: list[Name] = []
    for stmt in statements:
        if isinstance(stmt, (exp.Command, exp.Describe, exp.Use)) or type(stmt).__name__ == "Show":
            raise TableScopeViolation(
                "SHOW, DESCRIBE and similar statements are not allowed here. "
                "Read the enabled tables with a SELECT over <database>.<table> names."
            )
        for ref in tables_read(stmt, dialect):
            if ref not in refs:
                refs.append(ref)
    return refs


def one_part_table_refs(sql: str, dialect: str) -> list[str]:
    """Tables read without a database part; unreadable SQL and SHOW/DESCRIBE/USE raise, never `[]`."""
    return [ref[0] for ref in table_refs(sql, dialect) if len(ref) == 1]


def enforce_table_scope(client, query) -> None:
    """Check `query` against the scope attached to `client`, if any."""
    scope: TableScope | None = getattr(client, TABLE_SCOPE_ATTR, None)
    if not isinstance(scope, TableScope) or not isinstance(query, str) or not query.strip():
        return
    dialect = dialect_for_client(client)
    if not dialect:
        return
    check_table_scope(query, dialect, scope, getattr(client, "exact_table_scope_catalogs", None))
