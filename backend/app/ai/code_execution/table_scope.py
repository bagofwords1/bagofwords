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
other dialects' names case-insensitively.

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
        exact = None
        if lower:
            exact = cls.from_names(active, inactive, default_database=default_database,
                                   enforce_databases=enforce_databases, lower=False)
        return cls(active=act, inactive=ina, default_database=db, legacy=legacy, exact=exact)


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
    r"url|file|s3|gcs|hdfs|remote|remotesecure|mysql|postgresql|jdbc|odbc)$",
    re.IGNORECASE,
)


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
    """True when an unqualified table reference names a CTE visible where it
    stands, the two names compared as `dialect`'s engine compares them.

    A WITH clause is visible only inside the query it belongs to (and the
    queries nested in it), so a CTE defined in a subquery must not exempt a
    same-named table read in the outer query or in a sibling subquery. Inside
    a CTE's own body only the CTEs defined before it are visible, plus itself
    when the WITH is RECURSIVE.
    """
    from sqlglot import exp

    name = _name_part(table.this, dialect)
    child, inside_cte = table, None
    node = table.parent
    while node is not None:
        if isinstance(node, exp.CTE):
            inside_cte = node
        w = node.args.get("with") if isinstance(node, exp.Expression) else None
        if isinstance(w, exp.With):
            ctes = list(w.expressions)
            if child is w and inside_cte in ctes:
                i = ctes.index(inside_cte)
                visible = ctes[:i] + ([inside_cte] if w.args.get("recursive") else [])
            else:
                visible = ctes
            if any(_cte_name(c, dialect) == name for c in visible):
                return True
        child, node = node, node.parent
    return False


def _cte_name(cte, dialect: str | None) -> str:
    from sqlglot import exp

    alias = cte.args.get("alias")
    ident = alias.this if isinstance(alias, exp.TableAlias) else None
    return _name_part(ident, dialect) if isinstance(ident, exp.Identifier) else str(cte.alias_or_name).lower()


def tables_read(statement, dialect: str | None = None) -> list[Name]:
    """The tables `statement` reads by name, each once: every
    table reference that is an identifier (not a table function), except a
    one-part name that refers to a CTE visible where it stands. Name parts
    lowered, or for Postgres (`dialect`) folded as Postgres folds them."""
    from sqlglot import exp

    out: list[Name] = []
    for t in statement.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            continue
        ref = _table_ref(t, dialect)
        if (len(ref) == 1 and _resolves_to_cte(t, dialect)) or ref in out:
            continue
        out.append(ref)
    return out


def _display(ref: Name) -> str:
    return ".".join(p.upper() for p in ref)


def check_table_scope(sql: str, dialect: str, scope: TableScope) -> None:
    """Raise TableScopeViolation unless every table `sql` reads is active."""
    from app.ai.code_execution.sql_parser import parse_guard_sql
    from sqlglot import exp

    try:
        check_engine_reading(sql, dialect)
    except ReadOnlyViolation as e:
        raise TableScopeViolation(f"Could not verify which tables this query reads, so it was not run. {e}") from e
    try:
        statements = [s for s in parse_guard_sql(sql, dialect)
                      if s is not None and not isinstance(s, exp.Semicolon)]
    except Exception as e:  # any parser failure: the tables cannot be verified
        logger.info("table_scope: %s parser could not read query: %s", dialect, str(e)[:200])
        raise TableScopeViolation(
            "Could not verify which tables this query reads, so it was not run. "
            "Rewrite it as a plain SELECT over the agent's enabled tables."
        ) from e

    denied: list[str] = []
    for stmt in statements:
        if isinstance(stmt, exp.Use):
            continue  # the read-only guard only lets USE WAREHOUSE through
        if isinstance(stmt, exp.Command) or type(stmt).__name__ == "Show":
            verb = (str(stmt.this or "") if isinstance(stmt, exp.Command) else "SHOW").split(" ")[0].upper()
            raise TableScopeViolation(
                f"'{verb or 'This'}' statements are not allowed: this agent can only "
                "read its enabled tables, which are already described in its schema."
            )

        for fn in stmt.find_all(exp.Func):
            name = _func_name(fn)
            if name and _DENIED_FUNCTION.match(name):
                raise _unverifiable_function(name)
        # A denied name as a column or a field: the read-only guard's list,
        # which holds every Postgres entry above that takes one argument
        # (the large-object ones by exact name, so a column such as lo_price
        # still reads).
        field_denied = DENIED_FUNCTIONS.get(dialect)
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

        compared = scope.exact if dialect in _ENGINE_FOLDED and scope.exact is not None else scope
        for ref in tables_read(stmt, dialect):
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

    if denied:
        raise TableScopeViolation(
            f"Table access denied: {', '.join(denied)} "
            f"{'is' if len(denied) == 1 else 'are'} not enabled for this agent. "
            "Only the agent's selected tables can be queried; an admin can enable "
            "more in the agent's Tables settings. Do not retry with other syntax. "
            "Tell the user the table is not available to this agent."
        )


def enforce_table_scope(client, query) -> None:
    """Check `query` against the scope attached to `client`, if any."""
    scope: TableScope | None = getattr(client, TABLE_SCOPE_ATTR, None)
    if not isinstance(scope, TableScope) or not isinstance(query, str) or not query.strip():
        return
    dialect = dialect_for_client(client)
    if not dialect:
        return
    check_table_scope(query, dialect, scope)
