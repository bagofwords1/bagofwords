from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace

import sqlglot
from sqlglot import exp

from app.ai.code_execution.table_scope import _resolves_to_cte
from app.data_sources.clients.athena_unified_catalog import (
    TENANT_CASTS_KEY,
    TENANT_MISSING_KEY,
    TENANTS_KEY,
    athena_type,
)
from app.data_sources.clients.postgresql_multi_catalog import COLUMN_TYPES_KEY, TENANT_COLUMN

MAX_ATHENA_QUERY_BYTES = 262_144

_DIALECT = "athena"
_PLAIN_TABLE_ARGS = {"this", "alias", "db", "catalog"}


@dataclass(frozen=True)
class UnifiedTable:
    name: str
    columns: tuple[tuple[str, str], ...]
    tenants: tuple[str, ...]
    casts: Mapping[str, Mapping[str, str]]
    missing: Mapping[str, frozenset[str]]

    @classmethod
    def from_metadata(cls, name: str, metadata: dict) -> "UnifiedTable | None":
        tenants = (metadata or {}).get(TENANTS_KEY) or []
        if not tenants:
            return None
        column_types = metadata.get(COLUMN_TYPES_KEY) or {}
        return cls(
            name=name,
            columns=tuple((c, t) for c, t in column_types.items() if c != TENANT_COLUMN),
            tenants=tuple(tenants),
            casts={t: dict(c) for t, c in (metadata.get(TENANT_CASTS_KEY) or {}).items()},
            missing={t: frozenset(c) for t, c in (metadata.get(TENANT_MISSING_KEY) or {}).items()},
        )

    def restricted_to(self, keep: Callable[[str], bool]) -> "UnifiedTable | None":
        tenants = tuple(t for t in self.tenants if keep(t))
        if not tenants:
            return None
        return replace(
            self,
            tenants=tenants,
            casts={t: c for t, c in self.casts.items() if t in tenants},
            missing={t: c for t, c in self.missing.items() if t in tenants},
        )


@dataclass(frozen=True)
class Rewrite:
    sql: str
    branches: dict[str, tuple[str, ...]]
    columns: dict[str, tuple[str, ...]]
    emptied: tuple[str, ...] = ()
    # Left-out tenants the query's own tenant_db filter asked for, per table.
    named_left_out: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


class UnknownUnifiedTable(ValueError):
    pass


class UnifiedQueryTooLarge(ValueError):
    pass


class UnsupportedColumnType(ValueError):
    pass


@dataclass(frozen=True)
class _Reference:
    node: exp.Table
    table: UnifiedTable
    columns: tuple[tuple[str, str], ...]
    tenants: tuple[str, ...]
    emptied: bool
    named_left_out: tuple[str, ...] = ()


def rewrite_unified(
    sql: str,
    tables: Mapping[str, UnifiedTable],
    catalog: str,
    left_out: Mapping[str, frozenset[str]] | None = None,
) -> Rewrite:
    """`left_out` names, per table, the tenants that must not be read: dropped
    at query time, or left out at index time (type conflict, unreadable). A
    tenant_db filter that asks only for such tenants empties the reference,
    so the caller can refuse the query instead of returning no rows."""
    left_out = left_out or {}
    by_name = {name.lower(): table for name, table in tables.items()}
    statements = [s for s in sqlglot.parse(sql, read=_DIALECT) if s is not None]

    emitted: dict[str, set[str]] = {}
    emitted_columns: dict[str, dict[str, None]] = {}
    subqueries: list[tuple[UnifiedTable, exp.Subquery]] = []
    emptied: dict[str, None] = {}
    named_left_out: dict[str, dict[str, None]] = {}
    for statement in statements:
        references = [
            _plan(node, table, statement, left_out.get(table.name, frozenset()))
            for node, table in _unified_references(statement, by_name)
        ]
        for ref in references:
            subquery = exp.Subquery(this=_union(ref, catalog), alias=_alias(ref.node))
            ref.node.replace(subquery)
            subqueries.append((ref.table, subquery))
            emitted.setdefault(ref.table.name, set()).update(ref.tenants)
            emitted_columns.setdefault(ref.table.name, {}).update(dict.fromkeys(c for c, _ in ref.columns))
            if ref.emptied:
                emptied[ref.table.name] = None
            if ref.named_left_out:
                named_left_out.setdefault(ref.table.name, {}).update(dict.fromkeys(ref.named_left_out))

    final = ";\n".join(s.sql(dialect=_DIALECT) for s in statements)
    size = len(final.encode("utf-8"))
    if size > MAX_ATHENA_QUERY_BYTES:
        raise UnifiedQueryTooLarge(_too_large_message(size, subqueries, emitted))

    branches = {
        table.name: tuple(t for t in table.tenants if t in emitted[table.name])
        for table, _ in subqueries
    }
    columns = {table.name: tuple(emitted_columns[table.name]) for table, _ in subqueries}
    return Rewrite(
        sql=final, branches=branches, columns=columns, emptied=tuple(emptied),
        named_left_out={name: tuple(tenants) for name, tenants in named_left_out.items()},
    )


def _unified_references(statement: exp.Expression, by_name: Mapping[str, UnifiedTable]):
    for node in list(statement.find_all(exp.Table)):
        if not isinstance(node.this, exp.Identifier):
            continue
        if node.args.get("db") or node.args.get("catalog"):
            shown = ".".join(p for p in (node.catalog, node.db, node.name) if p)
            raise UnknownUnifiedTable(
                f"{shown} names a tenant database directly, which this connection does not "
                f"allow. Query the unified table {node.name} and filter on {TENANT_COLUMN} "
                "to read one tenant."
            )
        if _resolves_to_cte(node):
            continue
        table = by_name.get(node.name.lower())
        if table is None:
            raise UnknownUnifiedTable(
                f"{node.name} is not a unified table on this connection. Query only the "
                "unified tables listed in the schema."
            )
        extras = [k for k, v in node.args.items() if v and k not in _PLAIN_TABLE_ARGS]
        if extras:
            raise ValueError(
                f"{node.sql(dialect=_DIALECT)} is not supported on the unified table {table.name}. "
                "Query it as a plain table reference."
            )
        yield node, table


def _plan(
    node: exp.Table, table: UnifiedTable, statement: exp.Expression, left_out: frozenset[str]
) -> _Reference:
    alias = (node.alias or node.name).lower()
    owner, join = _introducing_select(node)
    alias_node = node.args.get("alias")
    renamed = alias_node is not None and bool(alias_node.columns)
    if (
        owner is None
        or renamed
        or _reads_every_column(statement, owner, alias)
    ):
        columns = table.columns
    else:
        used = _used_names(statement, alias)
        columns = tuple((c, t) for c, t in table.columns if c.lower() in used)

    allowed = None if renamed else _tenant_filter(owner, join, alias)
    candidates = tuple(t for t in table.tenants if allowed is None or t in allowed)
    # A filter naming a tenant left out at index time (so not in table.tenants)
    # still asked for it: count it, so a filter on only such tenants empties.
    named = tuple(sorted(t for t in allowed or () if t in left_out))
    candidates += tuple(t for t in named if t not in candidates)
    tenants = tuple(t for t in candidates if t not in left_out)
    return _Reference(
        node=node, table=table, columns=columns, tenants=tenants,
        emptied=bool(candidates) and not tenants, named_left_out=named,
    )


def _introducing_select(node: exp.Table) -> tuple[exp.Select | None, exp.Join | None]:
    parent = node.parent
    if isinstance(parent, exp.From) and isinstance(parent.parent, exp.Select):
        return parent.parent, None
    if isinstance(parent, exp.Join) and isinstance(parent.parent, exp.Select):
        return parent.parent, parent
    return None, None


def _reads_every_column(statement: exp.Expression, owner: exp.Select, alias: str) -> bool:
    if any(isinstance(e, exp.Star) for e in owner.expressions):
        return True
    if any(str(j.args.get("method") or "").upper() == "NATURAL" for j in owner.args.get("joins") or []):
        return True
    return any(
        isinstance(col.this, exp.Star) and col.table.lower() == alias
        for col in statement.find_all(exp.Column)
    )


def _used_names(statement: exp.Expression, alias: str) -> set[str]:
    qualifiers = {a.name.lower() for a in statement.find_all(exp.TableAlias)}
    qualifiers |= {t.name.lower() for t in statement.find_all(exp.Table)}
    names: set[str] = set()
    for col in statement.find_all(exp.Column):
        parts = [p.name.lower() for p in col.parts if isinstance(p, exp.Identifier)]
        if len(parts) == 1:
            names.add(parts[0])
        elif parts and parts[0] == alias:
            names.add(parts[1])
        elif parts and parts[0] not in qualifiers:
            names.update(parts)
    for join in statement.find_all(exp.Join):
        names.update(u.name.lower() for u in join.args.get("using") or [])
    return names


def _tenant_filter(owner: exp.Select | None, join: exp.Join | None, alias: str) -> set[str] | None:
    if owner is None:
        return None
    conditions: list[tuple[exp.Expression, bool]] = []
    where = owner.args.get("where")
    if where is not None:
        single_source = _source_count(owner) == 1
        conditions += [(c, single_source) for c in _conjuncts(where.this)]
    if join is not None and _on_filters_this_side(join):
        conditions += [(c, False) for c in _conjuncts(join.args["on"])]

    allowed: set[str] | None = None
    for condition, unqualified_counts in conditions:
        literals = _tenant_literals(condition, alias, unqualified_counts)
        if literals is not None:
            allowed = literals if allowed is None else allowed & literals
    return allowed


def _source_count(select: exp.Select) -> int:
    return (
        (1 if select.args.get("from") else 0)
        + len(select.args.get("joins") or [])
        + len(select.args.get("laterals") or [])
    )


def _on_filters_this_side(join: exp.Join) -> bool:
    side = str(join.side or "").upper()
    kind = str(join.kind or "").upper()
    return join.args.get("on") is not None and side in ("", "LEFT") and kind in ("", "INNER", "OUTER")


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    while isinstance(node, exp.Paren):
        node = node.this
    if node is None:
        return []
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _tenant_literals(condition: exp.Expression, alias: str, unqualified_counts: bool) -> set[str] | None:
    if isinstance(condition, exp.EQ):
        for column, literal in ((condition.this, condition.expression), (condition.expression, condition.this)):
            if _is_tenant_column(column, alias, unqualified_counts) and _is_string(literal):
                return {literal.this}
        return None
    if (
        isinstance(condition, exp.In)
        and _is_tenant_column(condition.this, alias, unqualified_counts)
        and not any(condition.args.get(k) for k in ("query", "unnest", "field"))
        and condition.expressions
        and all(_is_string(e) for e in condition.expressions)
    ):
        return {e.this for e in condition.expressions}
    return None


def _is_tenant_column(node: exp.Expression, alias: str, unqualified_counts: bool) -> bool:
    if not isinstance(node, exp.Column) or node.name.lower() != TENANT_COLUMN:
        return False
    if node.args.get("db") or node.args.get("catalog"):
        return False
    return node.table.lower() == alias if node.table else unqualified_counts


def _is_string(node: exp.Expression) -> bool:
    return isinstance(node, exp.Literal) and node.is_string


def _union(ref: _Reference, catalog: str) -> exp.Expression:
    if not ref.tenants:
        return _empty_relation(ref.table, ref.columns)
    branches = [_branch(ref.table, tenant, ref.columns, catalog) for tenant in ref.tenants]
    if len(branches) == 1:
        return branches[0]
    return exp.union(*branches, distinct=False, copy=False)


def _branch(table: UnifiedTable, tenant: str, columns, catalog: str) -> exp.Select:
    missing = table.missing.get(tenant, frozenset())
    casts = table.casts.get(tenant, {})
    projections: list[exp.Expression] = [
        exp.alias_(exp.Literal.string(tenant), TENANT_COLUMN, quoted=True)
    ]
    for name, dtype in columns:
        if name in missing:
            projections.append(exp.alias_(exp.cast(exp.null(), _data_type(dtype, table, name)), name, quoted=True))
        elif name in casts:
            projections.append(exp.alias_(exp.cast(_column(name), _data_type(dtype, table, name)), name, quoted=True))
        else:
            projections.append(_column(name))
    source = exp.Table(
        this=_identifier(table.name), db=_identifier(tenant), catalog=_identifier(catalog.lower())
    )
    return exp.select(*projections, copy=False).from_(source, copy=False)


def _empty_relation(table: UnifiedTable, columns) -> exp.Select:
    projections = [
        exp.alias_(exp.cast(exp.null(), _data_type("varchar", table, TENANT_COLUMN)), TENANT_COLUMN, quoted=True)
    ]
    projections += [
        exp.alias_(exp.cast(exp.null(), _data_type(dtype, table, name)), name, quoted=True) for name, dtype in columns
    ]
    return exp.select(*projections, copy=False).where(exp.false(), copy=False)


def _alias(node: exp.Table) -> exp.TableAlias:
    alias = node.args.get("alias")
    columns = [c.copy() for c in alias.columns] if alias is not None else []
    return exp.TableAlias(this=_identifier(node.alias or node.name), columns=columns or None)


def _identifier(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=True)


def _column(name: str) -> exp.Column:
    return exp.Column(this=_identifier(name))


def _data_type(dtype: str, table: UnifiedTable, column: str) -> exp.DataType:
    parsed = athena_type(dtype)
    if parsed is None:
        raise UnsupportedColumnType(
            f"The column {column} of the unified table {table.name} has a type this connection "
            "cannot query. Re-index this connection."
        )
    return parsed


def _too_large_message(size: int, subqueries, emitted: dict[str, set[str]]) -> str:
    contribution: dict[str, int] = {}
    for table, subquery in subqueries:
        contribution[table.name] = contribution.get(table.name, 0) + len(
            subquery.sql(dialect=_DIALECT).encode("utf-8")
        )
    largest = max(contribution, key=contribution.__getitem__) if contribution else None
    message = (
        f"This query is {size:,} bytes once unified tables are expanded, over Athena's "
        f"{MAX_ATHENA_QUERY_BYTES:,}-byte limit, so it was not sent."
    )
    if largest is not None:
        message += (
            f" Most of it is the unified table {largest}, unioned across "
            f"{len(emitted[largest])} tenant databases."
        )
    return message + (
        f" Select fewer columns, filter on {TENANT_COLUMN} (for example "
        f"WHERE {TENANT_COLUMN} = '<database>'), or narrow the connection's Database setting."
    )
