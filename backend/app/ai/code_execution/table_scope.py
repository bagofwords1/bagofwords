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
SQL clients without one (guard profile "nonparsing_sql": Db2, IBM i, Vertica,
SQL Anywhere, SAP HANA, Pinot) are checked while their selection disables any
table, on the text every reading of the engine's quoting rules agrees on,
parsed with one pinned dialect per engine and cross-checked token by token
(`check_nonparsing_table_scope`). Non-SQL clients (Mongo, Splunk, Salesforce,
API and MCP connectors) are not checked here. This is a second layer: the hard boundary is still the grants of
the database login behind the connection.
"""

from __future__ import annotations

import logging
import re
import string
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.ai.code_execution.client_profiles import guard_profile
from app.ai.code_execution.sql_guard import (
    DENIED_FUNCTIONS,
    ReadOnlyViolation,
    check_engine_reading,
    dialect_for_client,
    duckdb_read,
    nonparsing_normalized,
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
    # Spark, Databricks and Hive generators (LATERAL VIEW, LATERAL)
    "inline_outer", "json_tuple", "parse_url_tuple", "stack", "variant_explode",
    "variant_explode_outer",
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
    _check_statements(_parsed(sql, dialect), dialect, scope, exact_catalogs, denied)


def _parsed(sql: str, dialect: str) -> list:
    """The statements of `sql` as sqlglot's `dialect` parses them ("" is
    its generic dialect); a parse failure is refused."""
    from app.ai.code_execution.sql_parser import parse_guard_sql
    from sqlglot import exp

    try:
        return [s for s in parse_guard_sql(sql, dialect)
                if s is not None and not isinstance(s, exp.Semicolon)]
    except Exception as e:  # any parser failure: the tables cannot be verified
        logger.info("table_scope: %s parser could not read query: %s", dialect, str(e)[:200])
        raise TableScopeViolation(
            f"{_UNVERIFIED} Rewrite it as a plain SELECT over the agent's enabled tables."
        ) from e


def _check_statements(statements: list, dialect: str, scope: TableScope,
                      exact_catalogs: tuple[str, ...] | None, denied: list[str]) -> None:
    """The table scope checks on parsed `statements` (`check_table_scope`);
    `denied` holds the tables already found not enabled."""
    from sqlglot import exp

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

        # APPLY and LATERAL: over a subquery its tables are table references,
        # checked above; over a name or a call they are not. A call passes
        # as it would in FROM (a typed built-in or a row-producing function),
        # and so does T-SQL's nodes() method of an xml column.
        for lateral in stmt.find_all(exp.Lateral):
            target = lateral.this
            if isinstance(target, (exp.Subquery, exp.Query)):
                continue
            if isinstance(target, exp.Func) and (not isinstance(target, exp.Anonymous)
                                                 or _func_name(target).lower() in _SAFE_TABLE_FUNCTIONS):
                continue
            if dialect in _XML_NODES_DIALECTS and _is_xml_nodes_call(lateral):
                continue
            raise TableScopeViolation(
                f"'{lateral.sql(dialect=dialect)[:80]}' is not allowed as a query source, because "
                "the table it reads cannot be checked. Apply a subquery over the enabled tables instead."
            )

        _check_row_limits(stmt)

    _refuse_denied(denied)


# Dialects with the nodes() method of an xml column (`_is_xml_nodes_call`).
_XML_NODES_DIALECTS = frozenset({"tsql", "fabric"})


def _is_xml_nodes_call(lateral) -> bool:
    """Whether an APPLY's target is the nodes() method of an xml column,
    written `source.column.nodes('path')`: `source` is the alias (or, with
    no alias, the name) of a source of the same SELECT, and the one argument
    is a string. It makes rows from that column's value. Anything else
    before `.nodes(...)` may name a schema, and the call a function in it."""
    from sqlglot import exp

    target = lateral.this
    if not isinstance(target, exp.Dot):
        return False
    receiver, call = target.this, target.expression
    if not (isinstance(call, exp.Anonymous) and str(call.name).lower() == "nodes" and len(call.expressions) == 1
            and isinstance(call.expressions[0], exp.Literal) and call.expressions[0].is_string):
        return False
    if not (isinstance(receiver, exp.Dot) and isinstance(receiver.this, exp.Identifier)
            and isinstance(receiver.expression, exp.Identifier)):
        return False
    select = lateral.find_ancestor(exp.Select)
    if select is None:
        return False
    first = select.args.get("from") or select.args.get("from_")
    sources = [first.this] if first is not None else []
    sources += [join.this for join in select.args.get("joins") or ()]
    names = {str(s.alias_or_name).lower() for s in sources
             if s is not lateral and isinstance(s, (exp.Table, exp.Subquery, exp.Lateral))}
    return str(receiver.this.name).lower() in names - {""}


def _check_row_limits(statement) -> None:
    """Refuse a LIMIT, OFFSET, FETCH or TOP whose count is anything but a
    number or a bound parameter. An engine in which the clause word is no
    keyword reads it as the alias of the table before it, and a name after
    it as one more source, which no table reference here stands for."""
    from sqlglot import exp

    for node in statement.find_all(exp.Limit, exp.Offset, exp.Fetch):
        for key in ("expression", "offset", "count"):
            count = node.args.get(key)
            if count is None:
                continue
            plain = (isinstance(count, exp.Literal) and not count.is_string) or (
                isinstance(count, (exp.Placeholder, exp.Parameter))
                and count.find(exp.Column, exp.Identifier, exp.Query, exp.Func) is None)
            if not plain:
                raise TableScopeViolation(
                    f"{_UNVERIFIED} A row limit (LIMIT, OFFSET, FETCH, TOP) must be a plain number here: "
                    f"'{count.sql(dialect='')[:60]}' is not. Write the number of rows."
                )


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


NONPARSING_SCOPE_REFUSAL = (
    "The table selection on this connection cannot be checked for this connection type. "
    "Enable all tables on the connection to let the agent query it."
)

# ---------------------------------------------------------------------------
# SQL clients without a sqlglot dialect (guard profile "nonparsing_sql")
# ---------------------------------------------------------------------------

# The sqlglot dialect each engine's normalized text is parsed with (engine
# names as in `client_profiles`; IBM i is "db2"): Vertica's SQL as Postgres,
# SQL Anywhere's and HANA's as T-SQL, Db2's and Pinot's with sqlglot's
# generic dialect ("").
_NONPARSING_PARSE_DIALECTS = {"vertica": "postgres", "sybase": "tsql", "hana": "tsql", "db2": "", "pinot": ""}

# The tokens normalized text (`nonparsing_normalized`) may hold: whitespace,
# an emptied string, a quoted name, a number, a name, and plain SQL
# punctuation and operators. Anything else is refused.
_NONPARSING_TOKEN = re.compile(
    r"(?P<space>[ \t\r\n]+)"
    r"|(?P<string>'')"
    r"|(?P<quoted>\"[^\"]*\")"
    r"|(?P<number>(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<punct>::|<>|<=|>=|!=|\|\||[(),.;=<>*+\-/%|])"
)
_WORD_CHARS = frozenset(string.ascii_letters + string.digits + "_")

# Words refused anywhere outside strings and quoted names. TABLE: Db2's
# TABLE(...) and FINAL / NEW / OLD TABLE (no read names it otherwise).
# PLACEHOLDER: the parameters of a HANA calculation view.
_NONPARSING_DENIED_WORDS = {
    "db2": frozenset({"table"}),
    "vertica": frozenset({"table"}),
    "sybase": frozenset({"table"}),
    "hana": frozenset({"table", "placeholder"}),
    "pinot": frozenset({"table"}),
}
# The functions a query may call (a name before `(`) while a table is
# disabled: plain SQL functions that read only their arguments. Any other
# call is refused, naming it; so is a quoted name before `(` and a call
# written with its schema (`schema.function(...)`). Names are compared
# lowered, and on Pinot, which resolves a function by its name without
# underscores, without underscores too (`_canonical_call`).
_NONPARSING_CALLS = frozenset({
    # aggregates
    "count", "sum", "avg", "min", "max", "stddev", "stddev_pop", "stddev_samp", "variance", "var_pop",
    "var_samp",
    # conditionals
    "coalesce", "nullif", "greatest", "least",
    # strings
    "lower", "upper", "substr", "substring", "trim", "ltrim", "rtrim", "length", "char_length", "concat",
    "replace", "position", "left", "right", "lpad", "rpad",
    # math
    "abs", "round", "floor", "ceil", "ceiling", "mod", "power", "sqrt",
    # dates
    "current_date", "current_timestamp", "extract", "date_trunc",
    # conversion
    "cast", "convert",
    # window functions
    "row_number", "rank", "dense_rank", "lag", "lead", "first_value", "last_value", "ntile",
})
# More functions of each engine that read only their arguments there (IBM i
# is "db2", so its entry holds what both Db2 and IBM i have): date-add and
# date-diff, date parts, the current time, conversions, null handling,
# aggregates and window functions, math and strings. A name is listed only
# for an engine documented to have it as such a function.
_NONPARSING_ENGINE_CALLS = {
    "db2": frozenset({
        "add_days", "add_months", "add_years", "days_between", "months_between", "timestampdiff",
        "year", "month", "day", "hour", "minute", "second", "quarter", "week", "dayofweek", "date", "days", "now",
        "to_char", "to_date", "to_timestamp", "to_number", "int", "integer", "double",
        "ifnull", "nvl", "decode", "value",
        "listagg", "median", "percentile_cont", "cume_dist", "percent_rank",
        "sign", "exp", "ln", "log", "trunc", "rand",
        "instr", "locate", "initcap", "repeat", "lcase", "ucase", "digits", "regexp_like", "regexp_replace",
    }),
    "vertica": frozenset({
        "datediff", "timestampadd", "timestampdiff", "add_months", "months_between",
        "year", "month", "day", "hour", "minute", "second", "quarter", "week", "dayofweek", "date",
        "now", "getdate",
        "to_char", "to_date", "to_timestamp", "to_number",
        "ifnull", "nvl", "isnull", "zeroifnull", "decode",
        "listagg", "median", "percentile_cont", "cume_dist", "percent_rank",
        "sign", "exp", "ln", "log", "trunc",
        "instr", "initcap", "repeat", "regexp_like", "regexp_replace",
    }),
    "sybase": frozenset({
        "dateadd", "datediff",
        "year", "month", "day", "hour", "minute", "second", "quarter", "datepart", "date", "days", "dateformat",
        "now", "getdate",
        "to_char",
        "ifnull", "isnull",
        "list", "median", "percentile_cont", "cume_dist", "percent_rank",
        "sign", "exp", "log", "rand",
        "locate", "charindex", "reverse", "repeat", "lcase", "ucase",
    }),
    "hana": frozenset({
        "add_days", "add_months", "add_years", "add_seconds", "days_between", "seconds_between",
        "months_between", "years_between",
        "year", "month", "hour", "minute", "second", "quarter", "week", "now",
        "to_date", "to_timestamp", "to_varchar", "to_decimal",
        "ifnull",
        "string_agg", "median", "percentile_cont", "cume_dist", "percent_rank",
        "sign", "exp", "ln", "log", "rand",
        "locate", "initcap", "lcase", "ucase",
    }),
    "pinot": frozenset({
        "timestampadd", "timestampdiff",
        "year", "month", "day", "hour", "minute", "second", "quarter", "week", "dayofweek", "now", "ago",
        "datetimeconvert", "todatetime", "jsonextractscalar", "text_match",
        "distinctcount", "distinctcounthll", "percentile", "percentiletdigest",
        "sign", "exp", "ln",
        "reverse", "regexp_like", "regexp_replace",
    }),
}
# Type names written with a length or a precision (CAST(x AS DECIMAL(10, 2))).
_NONPARSING_TYPE_NAMES = frozenset({
    "decimal", "dec", "numeric", "char", "character", "varchar", "nchar", "nvarchar", "binary", "varbinary",
    "float", "time", "timestamp", "number", "decfloat",
})
# Keywords after which a `(` opens syntax, never a call.
_NONPARSING_KEYWORDS_BEFORE_PAREN = frozenset({
    "select", "from", "join", "on", "using", "where", "and", "or", "not", "in", "as", "over", "when", "then",
    "else", "values",
})
# Keywords after which a `(` may only open a subquery; before anything else
# the word is read as a function's name. APPLY and LATERAL take a row list
# (VALUES) as well, as a FROM clause does.
_NONPARSING_KEYWORDS_BEFORE_SUBQUERY = frozenset({"exists", "any", "some", "all", "apply", "lateral"})
# Engines with GROUP BY ROLLUP(...), CUBE(...) and GROUPING SETS (...).
_NONPARSING_GROUPING_ENGINES = frozenset({"db2", "vertica", "sybase", "hana"})
# Engines with `a IS [NOT] DISTINCT FROM b`, whose FROM opens no FROM clause.
_NONPARSING_IS_DISTINCT_FROM = frozenset({"db2", "pinot"})
# Words an expression may follow: before IS they are no left operand.
_WORDS_BEFORE_AN_EXPRESSION = frozenset({
    "select", "all", "distinct", "and", "or", "not", "when", "then", "else", "where", "on", "having", "by",
    "case", "in", "like", "between", "as", "is", "from", "join", "using", "values", "limit", "offset", "top",
    "first", "stream", "return", "set",
})
# Words after which a FROM clause has ended, at the depth it stands.
_FROM_CLAUSE_ENDS = frozenset({
    "where", "group", "having", "order", "limit", "fetch", "offset", "union", "except", "intersect", "minus",
    "window", "qualify", "for", "select", "values",
})
# An engine's one-row table, a source under this exact name only.
_NONPARSING_ONE_ROW_TABLES = {"hana": ("dummy",), "db2": ("sysibm", "sysdummy1")}
# The words of a Db2 labeled duration (`CURRENT DATE - 7 DAYS`).
_DB2_DURATION_UNITS = frozenset({
    "year", "years", "month", "months", "day", "days", "hour", "hours", "minute", "minutes", "second", "seconds",
    "microsecond", "microseconds",
})


def _disables_a_table(scope: TableScope) -> bool:
    """Whether any table is disabled in `scope`, in any of the spellings it
    compares (a name disabled only in another case still counts)."""
    return any(s is not None and s.inactive for s in (scope, scope.exact, scope.ascii_folded, scope.legacy))


def _names_collide_by_case(scope: TableScope) -> bool:
    """Whether a disabled name equals an enabled one once lowered: the engine
    may hold both (a quoted name keeps its case on Db2 and HANA), and a
    selection compared lowered cannot tell them apart."""
    if scope.exact is None:
        return False
    active = {tuple(p.lower() for p in name) for name in scope.exact.active}
    return any(tuple(p.lower() for p in name) in active for name in scope.exact.inactive)


def _nonparsing_refusal(reason: str) -> TableScopeViolation:
    return TableScopeViolation(f"{_UNVERIFIED} {reason}")


def _canonical_call(name: str, engine: str) -> str:
    """A function name as `engine` resolves it: lowered, and on Pinot
    without underscores."""
    name = name.lower()
    return name.replace("_", "") if engine == "pinot" else name


def _nonparsing_call_allowed(tokens: list[tuple[str, str]], i: int, engine: str, with_header: bool) -> bool:
    """Whether the unquoted word `tokens[i]`, which a `(` follows, may stand
    there:

      * a keyword after which the `(` opens syntax
        (`_NONPARSING_KEYWORDS_BEFORE_PAREN`), or opens a subquery
        (`_NONPARSING_KEYWORDS_BEFORE_SUBQUERY`, read from the token after
        the `(`); WITHIN GROUP (ORDER BY ...); GROUPING SETS, ROLLUP and
        CUBE where the engine has them;
      * a name given a column list, `(a, b)` and nothing else: after AS, or
        a WITH entry's name (`with_header`: the token stands among the
        entries of a WITH) before its AS;
      * a type name, or a function of `_NONPARSING_CALLS` /
        `_NONPARSING_ENGINE_CALLS`, each compared as `engine` resolves
        functions (`_canonical_call`)."""
    word = tokens[i][1].lower()
    before = tokens[i - 1][1].lower() if i else None
    inside = tokens[i + 2][1].lower() if i + 2 < len(tokens) else None
    if word in _NONPARSING_KEYWORDS_BEFORE_PAREN:
        return True
    if word in _NONPARSING_KEYWORDS_BEFORE_SUBQUERY:
        return inside in ("select", "with") or (inside == "values" and word in ("apply", "lateral"))
    if word == "group":
        return before == "within" and inside == "order"
    if engine in _NONPARSING_GROUPING_ENGINES and (word in ("rollup", "cube") or (word == "sets"
                                                                                 and before == "grouping")):
        return True
    after_list = _after_name_list(tokens, i + 1)
    if after_list is not None and (before == "as" or (with_header and after_list == "as")):
        return True
    allowed = _NONPARSING_CALLS | _NONPARSING_ENGINE_CALLS[engine] | _NONPARSING_TYPE_NAMES
    return _canonical_call(word, engine) in {_canonical_call(a, engine) for a in allowed}


def _after_name_list(tokens: list[tuple[str, str]], i: int) -> str | None:
    """With `tokens[i]` the `(` of a plain list of names (`(a, b)`: unquoted
    or quoted names and commas, nothing nested), the token after its `)`,
    lowered ("" at the end of the text); otherwise None."""
    j, n = i + 1, len(tokens)
    while j + 1 < n and tokens[j][0] in ("word", "quoted") and tokens[j + 1][1] == ",":
        j += 2
    if not (j + 1 < n and tokens[j][0] in ("word", "quoted") and tokens[j + 1][1] == ")"):
        return None
    return tokens[j + 2][1].lower() if j + 2 < n else ""


def _is_distinct_from(tokens: list[tuple[str, str]], i: int, engine: str) -> bool:
    """Whether the FROM at `tokens[i]` is the end of `a IS [NOT] DISTINCT
    FROM b` on an engine with that comparison: IS stands right after a left
    operand (a name, a number, a string or a `)`), so it cannot start a
    select item. The caller reads it so only after the FROM clause of the
    same SELECT."""
    if engine not in _NONPARSING_IS_DISTINCT_FROM or i < 3 or tokens[i - 1][1].lower() != "distinct":
        return False
    j = i - 2
    if tokens[j][1].lower() == "not":
        j -= 1
    if j < 1 or tokens[j][1].lower() != "is":
        return False
    kind, value = tokens[j - 1]
    if kind == "word":
        return value.lower() not in _WORDS_BEFORE_AN_EXPRESSION
    return kind in ("quoted", "number", "string") or value == ")"


def _nonparsing_tokens(text: str) -> list[tuple[str, str]]:
    """The tokens (kind, text) of normalized text, whitespace left out.
    Refused: a character outside `_NONPARSING_TOKEN` (so `@`, `#`, `$`, a
    backtick, a backslash, `[`, `?`, `{`, a lone `:` or `!`, and anything
    outside ASCII), a number with a name glued to it, a quoted name glued to
    a quote, `//` and `*/`, and a `;` before the last token."""
    tokens: list[tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        m = _NONPARSING_TOKEN.match(text, i)
        if m is None:
            c = text[i]
            shown = repr(c) if " " < c <= "~" else f"U+{ord(c):04X}"
            raise _nonparsing_refusal(
                f"It has {shown} outside a string or a quoted name, which the table checks for this source "
                "do not read. Remove it.")
        kind, value, end = m.lastgroup, m.group(), m.end()
        if kind == "number" and end < n and text[end] in _WORD_CHARS:
            raise _nonparsing_refusal(f"It has a name starting with a digit ({text[i:end + 1]}...). Quote the name.")
        if kind == "quoted" and end < n and text[end] in "'\"":
            raise _nonparsing_refusal("It has a quoted name directly followed by a quote. Put a space between them.")
        if value == "/" and i > 0 and text[i - 1] in "/*":
            raise _nonparsing_refusal(f"It has '{text[i - 1]}/' outside a string. Put a space between them.")
        if kind != "space":
            tokens.append((kind, value))
        i = end
    if any(value == ";" for _kind, value in tokens[:-1]):
        raise _nonparsing_refusal("Only a single statement is allowed.")
    return tokens


def _nonparsing_cross_check(tokens: list[tuple[str, str]], statements: list | None, scopes: tuple,
                            engine: str) -> None:
    """A check of the normalized tokens that does not use sqlglot:

      * each name after FROM, or after a comma in a FROM clause (the FROM of
        a SELECT at the same depth, in any subquery, WITH body or UNION
        branch), and each name after JOIN or APPLY wherever it stands
        (LATERAL before a name read as part of the clause), is a table or
        WITH entry the parse (`statements`) found; parentheses there hold a
        subquery, or FROM items checked the same way; a call, a `/` (IBM i
        system naming) or anything else there is refused;
      * a word that ends a FROM clause (`_FROM_CLAUSE_ENDS`) is not directly
        followed by a comma, nor LIMIT or OFFSET by a name: an engine that
        reads the word as the table's alias reads a source after it;
      * no name, quoted or not, equals the last part of a disabled table's
        name (in any of `scopes`), compared case-insensitively;
      * no word of `_NONPARSING_DENIED_WORDS`; every word before a `(` is
        one `_nonparsing_call_allowed` accepts, written unquoted and
        without a schema.

    With `statements` None (text that did not parse), everything but the
    comparison with the parse, to name a reason for the refusal."""
    from sqlglot import exp

    reported: set[Name] = set()
    for stmt in statements or ():
        for table in stmt.find_all(exp.Table):
            if isinstance(table.this, exp.Identifier):
                reported.add(tuple(p.lower() for p in (table.catalog, table.db, table.name) if p))
        for cte in stmt.find_all(exp.CTE):
            ident = _cte_ident(cte)
            if ident is not None:
                reported.add((str(ident.name).lower(),))
    disabled = frozenset(ref[-1].casefold() for s in scopes if s is not None for ref in s.inactive)
    denied_words = _NONPARSING_DENIED_WORDS[engine]

    def name_of(token):
        kind, value = token
        return value[1:-1] if kind == "quoted" else value if kind == "word" else None

    def check_name(name):
        if name.casefold() in disabled:
            raise TableScopeViolation(
                f"{name.upper()} is the name of a table that is not enabled for this agent. On this connection "
                "type a query may not use that name anywhere outside a string (as a column or an alias too). "
                "Query the agent's enabled tables without it.")

    # Per depth: a SELECT seen, inside its FROM clause, among a WITH's
    # entries, the SELECT's FROM clause seen.
    frames = [[False, False, False, False]]
    expect_item = False
    i, n = 0, len(tokens)
    while i < n:
        kind, value = tokens[i]
        nxt = tokens[i + 1][1] if i + 1 < n else None
        name = name_of(tokens[i])
        if name is not None:
            check_name(name)
        if kind == "word" and value.lower() in denied_words:
            raise _nonparsing_refusal(f"{value.upper()} is not allowed in agent queries on this connection type, "
                                      "because the table it reads cannot be checked.")
        if nxt == "(" and name is not None:
            if kind == "quoted":
                raise _nonparsing_refusal(
                    f"The function {name.upper()}() is written as a quoted name, which is not allowed on this "
                    "connection type. Call a plain SQL function by its unquoted name.")
            if not _nonparsing_call_allowed(tokens, i, engine, frames[-1][2]):
                raise _nonparsing_refusal(
                    f"The function {value.upper()}() is not allowed in agent queries on this connection type "
                    "while a table is disabled. Only plain SQL functions are: aggregates (COUNT, SUM, AVG, MIN, "
                    "MAX), COALESCE, NULLIF, string, math and date functions, CAST and window functions. "
                    "Rewrite the query without it.")
        if value == "(" and i >= 2 and tokens[i - 2][1] == "." and name_of(tokens[i - 1]) is not None:
            raise _nonparsing_refusal("A function called with its schema name is not allowed on this connection "
                                      "type, because the table it reads cannot be checked.")
        if expect_item and kind == "word" and value.lower() == "lateral":
            i += 1
            continue
        if expect_item:
            expect_item = False
            if value == "(":
                if nxt is not None and nxt.lower() in ("select", "with", "values"):
                    pass  # a subquery: its own FROM clauses are read below
                elif nxt == "(" or (i + 1 < n and name_of(tokens[i + 1]) is not None):
                    # Parentheses around a join, or around more parentheses:
                    # what they hold are FROM items, each checked as one.
                    frames.append([True, True, False, True])
                    expect_item = True
                    i += 1
                    continue
                else:
                    raise _nonparsing_refusal("In a FROM clause, parentheses may only hold a subquery or a join.")
            elif name is not None:
                parts, j = [name], i + 1
                while j + 1 < n and tokens[j][1] == "." and name_of(tokens[j + 1]) is not None:
                    parts.append(name_of(tokens[j + 1]))
                    check_name(parts[-1])
                    j += 2
                after = tokens[j][1] if j < n else None
                if after == "(":
                    raise _nonparsing_refusal(
                        f"'{'.'.join(parts)}(...)' is not allowed as a query source, because the table it reads "
                        "cannot be checked.")
                if after == "/":
                    raise _nonparsing_refusal(
                        "A table named LIBRARY/TABLE (system naming) is not read by the table checks. "
                        "Write LIBRARY.TABLE.")
                if statements is not None and tuple(p.lower() for p in parts) not in reported:
                    raise _nonparsing_refusal(f"'{'.'.join(parts)}' could not be read as a table. Rewrite the "
                                              "FROM clause as plain table names, joins and subqueries.")
                i = j
                continue
            else:
                raise _nonparsing_refusal(f"'{value}' is not allowed where a FROM clause names a table.")
        if value == "(":
            frames.append([False, False, False, False])
        elif value == ")":
            frames.pop()
            if not frames:
                raise _nonparsing_refusal("Its parentheses do not match.")
        elif kind == "word":
            word, frame = value.lower(), frames[-1]
            before = tokens[i - 1][1] if i else None
            if word == "select":
                frame[0], frame[1], frame[2], frame[3] = True, False, False, False
            elif word == "with" and not frame[0] and before in (None, "("):
                frame[2] = True
            elif word == "from" and frame[0]:
                # A SELECT has one FROM clause: only after it may a FROM be
                # the end of IS DISTINCT FROM.
                if not (frame[3] and _is_distinct_from(tokens, i, engine)):
                    frame[1] = expect_item = frame[3] = True
            elif word in ("join", "apply"):
                # Wherever it stands: after a word the engine may read as an
                # alias, the FROM clause goes on.
                frame[1] = expect_item = True
            elif word in _FROM_CLAUSE_ENDS and (before is None or before.lower() not in (".", "as")):
                # (After a dot or AS the word is a name, and the clause goes on.)
                if frame[1] and (nxt == "," or (word in ("limit", "offset") and i + 1 < n
                                                 and name_of(tokens[i + 1]) is not None)):
                    # To an engine in which the word is no keyword it is the
                    # alias of the table before it, and what follows it one
                    # more source.
                    raise _nonparsing_refusal(
                        f"'{value} {nxt}' cannot be read after a table in a FROM clause on this connection type. "
                        "Write the row limit as a number, and give the table another alias.")
                frame[1] = False
        elif value == "," and frames[-1][1]:
            expect_item = True
        i += 1
    if len(frames) != 1 or expect_item:
        raise _nonparsing_refusal("Its parentheses or its FROM clause are not complete.")


def _nonparsing_schema(client, engine: str) -> str | None:
    """The schema a qualified table name must name where the catalog stores
    bare names: a Vertica connection's schema, Pinot's default database.
    None where the catalog stores qualified names (or the schema is not
    known)."""
    if engine == "vertica":
        schema = getattr(client, "schema", None)
        return schema.lower() if isinstance(schema, str) and schema.strip() else ""
    if engine == "pinot":
        return "default"
    return None


def _schema_and_table(scope: TableScope) -> TableScope:
    """`scope` with each stored name read as `schema.table`: split at its
    first dot only, since the table part may hold dots. A reference then
    matches a table by its whole table part, never by a part of it."""
    def two_parts(names):
        return frozenset(name if len(name) <= 2 else (name[0], ".".join(name[1:])) for name in names)

    return TableScope(active=two_parts(scope.active), inactive=two_parts(scope.inactive) - two_parts(scope.active))


def _db2_parse_text(text: str) -> str:
    """Normalized Db2 text with the Db2 forms the generic parser does not
    read written as it reads them; only the parse gets this text, the token
    check and the engine get the query as written. None of the forms names
    a table:

      * a trailing isolation clause (WITH UR / CS / RS / RR) and FOR READ
        ONLY / FOR FETCH ONLY are left out;
      * CURRENT DATE / TIME / TIMESTAMP are written with an underscore;
      * the unit of a labeled duration after `+` or `-` (`- 7 DAYS`) is
        left out;
      * a leading WITH is read as WITH RECURSIVE: Db2 reads an entry's own
        name, in the later branches of its UNION, as the entry."""
    found = [m for m in _NONPARSING_TOKEN.finditer(text) if m.lastgroup != "space"]
    words = [m.group().lower() for m in found]
    edits: list[tuple[int, int, str]] = []
    end = len(found)
    if end and words[end - 1] == ";":
        end -= 1
    if end >= 2 and words[end - 2] == "with" and words[end - 1] in ("ur", "cs", "rs", "rr"):
        end -= 2
    if end >= 3 and words[end - 3] == "for" and words[end - 2] in ("read", "fetch") and words[end - 1] == "only":
        end -= 3
    if end < len(found):
        edits.append((found[end].start(), len(text), ""))
    for k in range(end - 1):
        if words[k] == "current" and words[k + 1] in ("date", "time", "timestamp"):
            edits.append((found[k].start(), found[k + 1].end(), "CURRENT_" + words[k + 1].upper()))
        elif (found[k].lastgroup == "number" and words[k + 1] in _DB2_DURATION_UNITS
              and k and words[k - 1] in ("+", "-")):
            edits.append((found[k + 1].start(), found[k + 1].end(), ""))
    if end >= 2 and words[0] == "with" and words[1] != "recursive":
        edits.append((found[0].end(), found[0].end(), " RECURSIVE"))
    for start, stop, new in sorted(edits, reverse=True):
        text = text[:start] + new + text[stop:]
    return text


def check_nonparsing_table_scope(sql: str, engine: str, scope: TableScope, schema: str | None = None) -> None:
    """Raise TableScopeViolation unless every table `sql` reads is active, for
    an engine sqlglot has no dialect for (`engine`, as in `client_profiles`).

      1. Every reading the engine may take of `sql` gives one normalized
         text (`nonparsing_normalized`), which holds only plain tokens
         (`_nonparsing_tokens`).
      2. That text (for Db2, `_db2_parse_text` of it), parsed with the
         engine's pinned dialect (`_NONPARSING_PARSE_DIALECTS`), passes the
         checks every parsed dialect gets (`_check_statements`); names are
         compared lowered.
      3. With `schema` (a catalog that stores bare names), a qualified table
         name must be `schema.table`; on Vertica with a schema other than
         `public`, a table name must be qualified. Without it (a catalog
         that stores `schema.table`, `_schema_and_table`), a name may not
         have more parts than the catalog's names, and matches a table by
         its whole table part.
      4. The engine's one-row table (`_NONPARSING_ONE_ROW_TABLES`) is a
         source under its exact name, unless the selection disables it.
      5. The token check `_nonparsing_cross_check` agrees."""
    from sqlglot import exp

    if engine not in _NONPARSING_PARSE_DIALECTS:
        raise TableScopeViolation(NONPARSING_SCOPE_REFUSAL)
    try:
        text = nonparsing_normalized(sql, engine)
    except ReadOnlyViolation as e:
        raise _nonparsing_refusal(str(e)) from e
    tokens = _nonparsing_tokens(text)
    dialect = _NONPARSING_PARSE_DIALECTS[engine]
    if dialect:
        try:
            check_engine_reading(text, dialect)
        except ReadOnlyViolation as e:
            raise _nonparsing_refusal(str(e)) from e
    # Compared lowered: Vertica reads quoted names case-insensitively too.
    stored = TableScope(active=scope.active, inactive=scope.inactive, default_database=scope.default_database)
    if schema is None:
        stored = _schema_and_table(stored)
    scopes = (scope, scope.exact, stored)
    try:
        statements = _parsed(_db2_parse_text(text) if engine == "db2" else text, dialect)
    except TableScopeViolation:
        _nonparsing_cross_check(tokens, None, scopes, engine)  # a more specific reason, if it finds one
        raise
    refs = list(dict.fromkeys(
        tuple(p.lower() for p in (t.catalog, t.db, t.name) if p)
        for stmt in statements for t in stmt.find_all(exp.Table) if isinstance(t.this, exp.Identifier)))
    width = max((len(ref) for ref in stored.active | stored.inactive), default=0)
    one_row = _NONPARSING_ONE_ROW_TABLES.get(engine)
    if one_row is not None and one_row not in stored.inactive:
        # The one-row table under its exact name; any other name that ends
        # like it is a table of the catalog or refused.
        _refuse_denied([_display(ref) for ref in refs
                        if ref != one_row and ref[-1] == one_row[-1] and not is_allowed(ref, stored)])
        refs = [ref for ref in refs if ref != one_row]
        stored = TableScope(active=stored.active | {one_row}, inactive=stored.inactive)
    if schema is not None:
        _refuse_denied([_display(ref) for ref in refs if len(ref) > 1 and (len(ref) != 2 or ref[0] != schema)])
    else:
        # A name with more parts than the catalog's (`schema.table` at most)
        # may be the same table in another database.
        _refuse_denied([_display(ref) for ref in refs if len(ref) > width])
    if engine == "vertica" and schema not in (None, "", "public"):
        # Vertica reads a bare name through its search path, not the
        # connection's schema.
        bare = [ref[0] for stmt in statements for ref in tables_read(stmt, dialect) if len(ref) == 1]
        if bare:
            raise TableScopeViolation(
                f"{bare[0].upper()} is not qualified with its schema, so this source may read it from another "
                f"schema. Write {schema.upper()}.{bare[0].upper()}.")
    for stmt in statements:
        _check_names_of_disabled_tables(stmt, scope)  # with the stored names split at every dot too
    _check_statements(statements, dialect, stored, None, [])
    _nonparsing_cross_check(tokens, statements, scopes, engine)


def enforce_table_scope(client, query) -> None:
    """Check `query` against the scope attached to `client`, if any.

    A client whose SQL sqlglot has no dialect for (guard profile
    "nonparsing_sql") is checked by `check_nonparsing_table_scope` while its
    scope disables any table, and is not checked otherwise; while a disabled
    name equals an enabled one once lowered (`_names_collide_by_case`), every
    query is refused. Non-SQL clients are not checked.
    """
    scope: TableScope | None = getattr(client, TABLE_SCOPE_ATTR, None)
    if not isinstance(scope, TableScope) or not isinstance(query, str) or not query.strip():
        return
    dialect = dialect_for_client(client)
    if not dialect:
        profile = guard_profile(client)
        if profile is not None and profile.kind == "nonparsing_sql" and _disables_a_table(scope):
            if _names_collide_by_case(scope):
                raise TableScopeViolation(NONPARSING_SCOPE_REFUSAL)
            check_nonparsing_table_scope(query, profile.engine, scope, _nonparsing_schema(client, profile.engine))
        return
    check_table_scope(query, dialect, scope, getattr(client, "exact_table_scope_catalogs", None))
