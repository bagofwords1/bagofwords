"""Query subsumption for the Result Lake.

Goal: serve a *narrower* single-table query from a cached **full table scan**
without re-querying the source. Example: if `SELECT * FROM orders` is cached, then
`SELECT region, SUM(amount) FROM orders WHERE country='US' GROUP BY region` can be
computed by running that SQL against the cached Parquet via DuckDB.

Safety first — a miss is always correct, a wrong rewrite is not. We only subsume
when it is *provably* sound:

  * The cached query is a FULL SCAN of a single table: `SELECT <cols|*> FROM t`
    with NO WHERE / GROUP BY / HAVING / DISTINCT / LIMIT / QUALIFY and no
    aggregate/window expressions. Such a result contains every row of `t`.
  * The new query reads that SAME single table (no joins, no subqueries in FROM).
  * Every column the new query references exists in the cached result
    (cached `SELECT *`, or the new query's columns ⊆ the cached column list).

Under those conditions the cached Parquet holds every row and every needed column,
so running the new query against it yields exactly the source result. Anything we
can't analyze, or any execution error, falls back to a normal miss.

Requires `sqlglot`. If it is unavailable, `analyze` returns a non-analyzable shape
and subsumption is silently disabled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import FrozenSet, Optional, Union

logger = logging.getLogger(__name__)

STAR = "*"  # sentinel: cached query selected all columns


@dataclass(frozen=True)
class QueryShape:
    analyzable: bool = False
    single_table: bool = False
    base_table: Optional[str] = None          # normalized catalog.db.name
    is_full_scan: bool = False                # safe to subsume FROM
    columns: Optional[Union[str, FrozenSet[str]]] = None  # STAR or explicit set (full-scan only)
    referenced_columns: FrozenSet[str] = frozenset()


def _normalized_table(table) -> str:
    parts = [getattr(table, "catalog", None), getattr(table, "db", None), table.name]
    return ".".join(p.lower() for p in parts if p)


def analyze(sql: str) -> QueryShape:
    """Best-effort structural analysis of a SELECT. Conservative: any unsupported
    shape or parse failure yields analyzable=False."""
    try:
        import sqlglot
        from sqlglot import expressions as exp
    except Exception:
        return QueryShape()

    try:
        tree = sqlglot.parse_one(sql)
    except Exception:
        return QueryShape()

    if not isinstance(tree, exp.Select):
        return QueryShape()

    from_ = tree.find(exp.From)
    if from_ is None:
        return QueryShape()
    base = from_.this
    has_joins = tree.find(exp.Join) is not None
    single_table = isinstance(base, exp.Table) and not has_joins
    base_table = _normalized_table(base) if isinstance(base, exp.Table) else None

    referenced_columns = frozenset(c.name.lower() for c in tree.find_all(exp.Column) if c.name)

    # Full-scan determination (matters when THIS query is a cache entry).
    is_full_scan = False
    columns: Optional[Union[str, FrozenSet[str]]] = None
    if single_table:
        no_row_changing_clause = all(
            tree.find(node) is None
            for node in (exp.Where, exp.Group, exp.Having, exp.Qualify, exp.Limit, exp.Distinct)
        )
        # No aggregate or window functions in the projection.
        no_agg = not any(
            e.find(exp.AggFunc) is not None or e.find(exp.Window) is not None
            for e in tree.expressions
        )
        if no_row_changing_clause and no_agg:
            if any(isinstance(e, exp.Star) for e in tree.expressions):
                is_full_scan = True
                columns = STAR
            else:
                names = set()
                plain = True
                for e in tree.expressions:
                    col = e if isinstance(e, exp.Column) else (
                        e.this if isinstance(e, exp.Alias) and isinstance(e.this, exp.Column) else None
                    )
                    if col is None:
                        plain = False
                        break
                    names.add(col.name.lower())
                if plain:
                    is_full_scan = True
                    columns = frozenset(names)

    return QueryShape(
        analyzable=True,
        single_table=single_table,
        base_table=base_table,
        is_full_scan=is_full_scan,
        columns=columns,
        referenced_columns=referenced_columns,
    )


def can_subsume(cached: QueryShape, new: QueryShape) -> bool:
    """True if `new` can be answered entirely from a cached full-scan `cached`."""
    if not (cached.is_full_scan and cached.base_table):
        return False
    if not (new.analyzable and new.single_table and new.base_table):
        return False
    if cached.base_table != new.base_table:
        return False
    if cached.columns == STAR:
        return True
    if isinstance(cached.columns, frozenset):
        return new.referenced_columns <= cached.columns
    return False


def rewrite_onto_parquet(new_sql: str, parquet_path: str) -> Optional[str]:
    """Rewrite `new_sql` so its single base table reads from `parquet_path` (DuckDB
    read_parquet). Returns DuckDB SQL, or None if the rewrite can't be built."""
    try:
        import sqlglot
        from sqlglot import expressions as exp

        tree = sqlglot.parse_one(new_sql)
        from_ = tree.find(exp.From)
        if from_ is None:
            return None
        base = from_.this
        if not isinstance(base, exp.Table):
            return None
        alias = base.alias or base.name
        esc = str(parquet_path).replace("'", "''")
        repl = sqlglot.parse_one(f"SELECT * FROM read_parquet('{esc}')").subquery(alias=alias)
        base.replace(repl)
        return tree.sql(dialect="duckdb")
    except Exception:
        logger.debug("subsumption rewrite failed", exc_info=True)
        return None
