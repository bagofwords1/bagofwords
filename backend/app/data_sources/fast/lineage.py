"""Where each column of a custom table comes from.

Given a custom table's definition and the connection's already-indexed
catalog, say for each output column either the source column it is a plain
copy of, or that it is derived and from what expression. Nothing here talks
to a source: the catalog is what indexing stored.

Three families of definition:

  * SQL (every connector sqlglot has a dialect for): a plain or aliased
    column traces to `table.column`; an aggregate, CASE, cast or arithmetic is
    derived and keeps its expression text.
  * DAX (Power BI, Analysis Services): `Table[Column]` and `[Measure]`
    references are matched against the indexed model.
  * Field lists (Priority's OData, a BusinessObjects universe query): the
    selected fields map one to one.

The rule throughout: **never guess**. A column whose origin cannot be read
unambiguously gets no lineage at all, because a missing description is
better than a wrong one. Every function here returns an empty result rather
than raising, whatever it is given.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

KIND_COLUMN = "column"     # a plain copy of an indexed column
KIND_MEASURE = "measure"   # a model measure, evaluated at this table's grain
KIND_DERIVED = "derived"   # computed in the definition

FAMILY_SQL = "sql"
FAMILY_DAX = "dax"
FAMILY_FIELDS = "fields"

# Connection type -> sqlglot dialect. Types absent here are still tried as
# generic SQL when their definition parses as one SELECT.
SQL_DIALECTS = {
    "postgresql": "postgres", "aws_redshift": "redshift", "mysql": "mysql",
    "mariadb": "mysql", "mssql": "tsql", "ms_fabric": "tsql",
    "oracledb": "oracle", "sqlite": "sqlite", "snowflake": "snowflake",
    "bigquery": "bigquery", "aws_athena": "athena", "presto": "presto",
    "trino": "trino", "clickhouse": "clickhouse",
    "databricks_sql": "databricks", "duckdb": "duckdb", "teradata": "teradata",
    "druid": "druid", "dremio": "dremio", "hive": "hive", "sybase": "tsql",
}
DAX_TYPES = {"powerbi", "analysis_services"}
FIELD_TYPES = {"priority_erp", "businessobjects"}


def family_of(conn_type) -> str:
    kind = (conn_type or "").lower()
    if kind in DAX_TYPES:
        return FAMILY_DAX
    if kind in FIELD_TYPES:
        return FAMILY_FIELDS
    return FAMILY_SQL


@dataclass(frozen=True)
class SourceRef:
    """An indexed column (or measure): the catalog table's name as stored and
    the column's name as stored."""
    table: str
    column: str


@dataclass
class ColumnLineage:
    kind: str
    source: Optional[SourceRef] = None       # KIND_COLUMN / KIND_MEASURE
    expression: Optional[str] = None         # KIND_DERIVED
    # Indexed columns a derived expression reads, where they could be named.
    reads: list = field(default_factory=list)
    # A grouping key of an aggregating query: part of the table's grain.
    grouping: bool = False
    # The parsed expression, for the additivity classifier (SQL only).
    node: object = None


@dataclass
class Lineage:
    family: str = FAMILY_SQL
    dialect: Optional[str] = None
    # Output column name (as stored on the custom table) -> its lineage.
    columns: dict = field(default_factory=dict)
    # Catalog tables the definition reads, by stored name.
    tables: list = field(default_factory=list)
    # Whether the definition aggregates (GROUP BY, SUMMARIZECOLUMNS, ...).
    aggregated: bool = False
    # How outputs are found before they are aligned to the stored names: an
    # ordered list (SQL, field lists) or a by-name lookup (DAX, whose output
    # names the engine produces).
    ordered: list = field(default_factory=list, repr=False)
    positional: bool = False
    lookup: object = field(default=None, repr=False)


class Catalog:
    """The connection's indexed tables, looked up the way definitions name
    them. Built from plain dicts: `{"name", "columns", "pks", "fks",
    "metadata_json"}`."""

    def __init__(self, tables: Iterable[dict]):
        self.tables = [t for t in (tables or []) if isinstance(t, dict) and t.get("name")]
        self._by_parts: dict = {}
        for t in self.tables:
            parts = _name_parts(t["name"])
            for i in range(len(parts)):
                self._by_parts.setdefault(parts[i:], []).append(t)

    def table(self, name_parts: tuple) -> Optional[dict]:
        """The one table a (possibly partly qualified) name means, or None
        when there is none or more than one."""
        hits = self._by_parts.get(tuple(p.lower() for p in name_parts if p), [])
        return hits[0] if len(hits) == 1 else None

    @staticmethod
    def column(table: dict, name: str) -> Optional[dict]:
        wanted = (name or "").lower()
        hits = [c for c in (table.get("columns") or [])
                if isinstance(c, dict) and str(c.get("name") or "").lower() == wanted]
        return hits[0] if len(hits) == 1 else None


def _name_parts(name: str) -> tuple:
    return tuple(p.strip().strip('"`[]').lower() for p in str(name).split(".") if p.strip())


def mentions(definition: str, table_name: str, metadata_json=None) -> bool:
    """Cheap test that `definition` could reference a catalog table, so a
    caller can load columns only for the tables that matter. A superset: a
    false positive costs one row, a false negative loses lineage."""
    text = (definition or "").lower()
    names = {str(table_name).lower(), _name_parts(table_name)[-1] if _name_parts(table_name) else ""}
    names.add(str(table_name).split("/")[-1].strip().lower())
    for key in ("powerbi", "analysis_services", "businessobjects"):
        meta = (metadata_json or {}).get(key) if isinstance(metadata_json, dict) else None
        if isinstance(meta, dict):
            for k in ("tableName", "universe_name", "universe_id"):
                if meta.get(k):
                    names.add(str(meta[k]).lower())
    return any(n and n in text for n in names)


def trace(definition: str, tables: Iterable[dict], conn_type: str,
          output_columns: Optional[list] = None, target: Optional[dict] = None) -> Lineage:
    """The lineage of `definition` against the indexed `tables`.

    `output_columns` are the column names the custom table actually stores, in
    order. Sources rename (duplicates get a suffix, some fold case), so
    lineage is aligned to these names by position where the count agrees and
    by unambiguous name otherwise.
    """
    family = family_of(conn_type)
    catalog = Catalog(tables)
    try:
        if family == FAMILY_DAX:
            found = _trace_dax(definition or "", catalog, target)
        elif family == FAMILY_FIELDS:
            found = _trace_fields(definition or "", catalog, (conn_type or "").lower())
        else:
            found = _trace_sql(definition or "", catalog, SQL_DIALECTS.get((conn_type or "").lower()))
    except Exception:
        # Lineage is an enrichment; a definition we cannot read is not an error.
        logger.info("custom_table.lineage.unreadable", exc_info=True)
        return Lineage(family=family)
    found.family = family
    if output_columns is not None:
        found.columns = _align(found, [str(c) for c in output_columns])
    else:
        found.columns = _unique_by_name(found.ordered)
    return found


def _unique_by_name(ordered: list) -> dict:
    seen: dict = {}
    for name, lin in ordered:
        seen.setdefault(str(name).lower(), []).append((name, lin))
    return {hits[0][0]: hits[0][1] for hits in seen.values() if len(hits) == 1 and hits[0][1] is not None}


def _align(found: Lineage, output: list) -> dict:
    lowered = [o.lower() for o in output]
    if found.lookup is not None:
        result = {}
        for name in output:
            lin = found.lookup(name) if lowered.count(name.lower()) == 1 else None
            if lin is not None:
                result[name] = lin
        return result
    if found.positional and len(found.ordered) == len(output):
        return {out: lin for out, (_, lin) in zip(output, found.ordered) if lin is not None}
    by_name = {k.lower(): v for k, v in _unique_by_name(found.ordered).items()}
    return {
        out: by_name[out.lower()] for out in output
        if out.lower() in by_name and lowered.count(out.lower()) == 1
    }


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------

def _trace_sql(sql: str, catalog: Catalog, dialect: Optional[str]) -> Lineage:
    import sqlglot
    from sqlglot import exp

    out = Lineage(dialect=dialect)
    text = re.sub(r"\{\{\s*(since|until)\s*\}\}", "NULL", sql.strip().rstrip(";"), flags=re.IGNORECASE)
    try:
        statements = sqlglot.parse(text, read=dialect)
    except Exception:
        return out
    if len(statements) != 1 or statements[0] is None:
        return out
    tree = statements[0]
    if isinstance(tree, exp.Subquery):
        tree = tree.unnest()
    if not isinstance(tree, exp.Select):
        # UNION / INTERSECT / VALUES / a command: a column there has more than
        # one origin, or none we can name.
        return out

    ctes = {}
    with_ = tree.args.get("with") or tree.args.get("with_")
    if with_ is not None:
        if with_.args.get("recursive"):
            return out
        for cte in with_.expressions:
            ctes[cte.alias_or_name.lower()] = cte.this

    used: list = []
    ordered, positional = _select_outputs(tree, catalog, ctes, dialect, used, depth=0)
    out.ordered, out.positional = ordered, positional
    out.tables = list(dict.fromkeys(used))
    out.aggregated = _aggregates(tree)
    return out


def _aggregates(select) -> bool:
    from sqlglot import exp

    if select.args.get("group"):
        return True
    for projection in select.expressions:
        for agg in projection.find_all(exp.AggFunc):
            if not agg.find_ancestor(exp.Window, exp.Subquery):
                return True
    return False


class _Relation:
    """One FROM-clause entry of a SELECT: a catalog table, or a subquery /
    CTE whose own outputs have been traced."""

    def __init__(self, table: Optional[dict] = None, outputs: Optional[dict] = None, known: bool = True,
                 base: bool = False):
        self.table, self.outputs, self.known = table, outputs, known
        # A real table of the source (indexed or not), as opposed to a
        # subquery, a CTE or a table function: its columns are stored values.
        self.base = base

    def has(self, column: str) -> Optional[bool]:
        """True / False when it can be told, None when the relation's columns
        are not known (an unindexed table, an unreadable subquery)."""
        if not self.known:
            return None
        if self.table is not None:
            return Catalog.column(self.table, column) is not None
        return column.lower() in (self.outputs or {})


def _select_outputs(select, catalog: Catalog, ctes: dict, dialect, used: list, depth: int):
    """[(output name, ColumnLineage | None)] for one SELECT, and whether the
    list is complete and in order (so it may be matched by position)."""
    from sqlglot import exp

    if depth > 8:
        return [], False

    relations: dict = {}   # alias (lower) -> _Relation
    order: list = []

    def add(node):
        if isinstance(node, exp.Table):
            alias = (node.alias or node.name or "").lower()
            name = node.name.lower()
            if not node.args.get("db") and name in ctes:
                relations[alias] = _from_query(ctes[name])
            else:
                parts = tuple(p.name for p in (node.args.get("catalog"), node.args.get("db"), node.this)
                              if p is not None and getattr(p, "name", ""))
                table = catalog.table(parts)
                if table is not None:
                    used.append(table["name"])
                relations[alias] = _Relation(table=table, known=table is not None, base=True)
            order.append(alias)
        elif isinstance(node, exp.Subquery):
            alias = (node.alias or "").lower()
            relations[alias] = _from_query(node.this)
            order.append(alias)
        else:
            # A table function, VALUES, UNNEST, a pivot: columns unknown.
            alias = (getattr(node, "alias", "") or f"_rel{len(order)}").lower()
            relations[alias] = _Relation(known=False)
            order.append(alias)

    def _from_query(query):
        if not isinstance(query, exp.Select):
            return _Relation(known=False)
        inner, complete = _select_outputs(query, catalog, ctes, dialect, used, depth + 1)
        if not complete:
            return _Relation(known=False)
        return _Relation(outputs={k.lower(): v for k, v in _unique_by_name(inner).items()},
                         known=len(_unique_by_name(inner)) == len(inner))

    from_ = select.args.get("from") or select.args.get("from_")
    if from_ is None:
        return [], False
    add(from_.this)
    for join in select.args.get("joins") or []:
        add(join.this)
    if any(isinstance(lat, exp.Lateral) for lat in select.args.get("laterals") or []):
        return [], False

    def relation_of(col):
        """The FROM-clause entry a column reference reads, or None when that
        cannot be told."""
        name = col.name
        if not name or isinstance(col.this, exp.Star):
            return None
        qualifier = (col.table or "").lower()
        if qualifier:
            return relations.get(qualifier)
        owners = [relations[a] for a in order if relations[a].has(name) is True]
        unknown = [relations[a] for a in order if relations[a].has(name) is None]
        # An unqualified name could belong to a relation we cannot see
        # into; with one in the FROM clause it is not ours to assign.
        if len(owners) != 1 or unknown:
            return None
        return owners[0]

    def stored_value(col) -> bool:
        """Whether a column reference is certainly a row-level value of a
        source table, not something another query computed."""
        rel = relation_of(col)
        if rel is not None:
            return rel.base
        return bool(order) and all(relations[a].base for a in order)

    def computation(inner):
        """`inner` with every reference to another query's output replaced by
        what that query computes, so the additivity of `CAST(users AS BIGINT)`
        is decided by what `users` is. An output whose computation is not
        known becomes a marker the classifier reads as unknown."""
        from app.data_sources.fast.additivity import UNRESOLVED_INPUT

        def swap(n):
            if not isinstance(n, exp.Column) or isinstance(n.this, exp.Star):
                return n
            if stored_value(n):
                return n
            ref = resolve(n)
            if ref is not None and ref.kind == KIND_COLUMN:
                return n
            if ref is not None and ref.node is not None:
                return exp.Paren(this=ref.node.copy())
            return exp.Anonymous(this=UNRESOLVED_INPUT)

        return inner.copy().transform(swap)

    def resolve(col) -> Optional[ColumnLineage]:
        """The lineage of one column reference, or None."""
        name = col.name
        rel = relation_of(col)
        if rel is None:
            return None
        if rel.table is not None:
            hit = Catalog.column(rel.table, name)
            if hit is None:
                return None
            return ColumnLineage(KIND_COLUMN, source=SourceRef(rel.table["name"], hit["name"]))
        if rel.outputs is not None:
            inner = rel.outputs.get(name.lower())
            if inner is None:
                return None
            # What the inner query grouped by is still a key out here: a year
            # that came through a CTE is no more a measure than it was inside.
            return ColumnLineage(inner.kind, inner.source, inner.expression, list(inner.reads),
                                 grouping=inner.grouping, node=inner.node)
        return None

    group_keys = set()
    group = select.args.get("group")
    if group is not None:
        for key in group.expressions:
            if isinstance(key, exp.Column):
                group_keys.add(key.sql(dialect=dialect).lower())
                group_keys.add(key.name.lower())
            elif isinstance(key, exp.Literal) and key.is_int:
                group_keys.add(int(key.name))
            else:
                group_keys.add(key.sql(dialect=dialect).lower())

    outputs: list = []
    complete = True
    for index, projection in enumerate(select.expressions, start=1):
        inner = projection.unalias() if isinstance(projection, exp.Alias) else projection
        while isinstance(inner, exp.Paren):
            inner = inner.this
        if isinstance(inner, exp.Star) or (isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star)):
            qualifier = inner.table.lower() if isinstance(inner, exp.Column) else ""
            targets = [qualifier] if qualifier else order
            if inner.args.get("except") or inner.args.get("replace") or inner.args.get("rename"):
                complete = False
                continue
            for alias in targets:
                rel = relations.get(alias)
                if rel is None or not rel.known:
                    complete = False
                    continue
                if rel.table is not None:
                    for c in rel.table.get("columns") or []:
                        if isinstance(c, dict) and c.get("name"):
                            outputs.append((c["name"], ColumnLineage(
                                KIND_COLUMN, source=SourceRef(rel.table["name"], c["name"]))))
                else:
                    outputs.extend((k, v) for k, v in (rel.outputs or {}).items())
            continue

        name = projection.alias_or_name or ""
        grouping = (
            index in group_keys
            or inner.sql(dialect=dialect).lower() in group_keys
            or (bool(name) and name.lower() in group_keys and not inner.find(exp.AggFunc))
        )
        if isinstance(inner, exp.Column):
            lin = resolve(inner)
            if lin is not None:
                lin.grouping = lin.grouping or grouping or (
                    lin.kind == KIND_COLUMN and bool(group_keys) and inner.name.lower() in group_keys)
            outputs.append((name or inner.name, lin))
            continue

        reads = []
        for col in inner.find_all(exp.Column):
            ref = resolve(col)
            if ref is not None and ref.source is not None and ref.source not in reads:
                reads.append(ref.source)
        outputs.append((name, ColumnLineage(
            KIND_DERIVED, expression=inner.sql(dialect=dialect), reads=reads,
            grouping=grouping, node=computation(inner),
        )))
    return outputs, complete


# --------------------------------------------------------------------------
# DAX
# --------------------------------------------------------------------------

_DAX_TOKEN = re.compile(
    r"""
      (?P<block_comment>/\*.*?\*/)
    | (?P<line_comment>(?://|--)[^\n]*)
    | (?P<string>"(?:[^"]|"")*")
    | (?P<quoted>'(?:[^']|'')*')
    | (?P<bracket>\[(?:[^\]]|\]\])*\])
    | (?P<word>[A-Za-z_][\w.]*)
    | (?P<number>\d+(?:\.\d+)?)
    | (?P<punct>[(),{}=<>+\-*/&|^!:;])
    | (?P<space>\s+)
    | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)
_DAX_NAMING = {"summarizecolumns", "addcolumns", "selectcolumns", "summarize", "row", "groupby"}
_DAX_AGGREGATING = {"summarizecolumns", "summarize", "groupby", "row"}


def dax_tokens(text: str) -> list:
    """(kind, text) tokens with comments and whitespace removed."""
    return [
        (m.lastgroup, m.group()) for m in _DAX_TOKEN.finditer(text or "")
        if m.lastgroup not in ("block_comment", "line_comment", "space")
    ]


def _dax_text(tokens: list) -> str:
    out = ""
    for kind, text in tokens:
        if out and kind in ("word", "number", "string", "quoted") and out[-1:].isalnum():
            out += " "
        out += text
        if text == ",":
            out += " "
    return out.strip()


def _bracket_name(token_text: str) -> str:
    return token_text[1:-1].replace("]]", "]").strip()


def _table_name(kind: str, token_text: str) -> str:
    return token_text[1:-1].replace("''", "'").strip() if kind == "quoted" else token_text.strip()


def _dax_models(catalog: Catalog) -> dict:
    """model id -> {internal table name (lower): catalog table}."""
    models: dict = {}
    for t in catalog.tables:
        meta = t.get("metadata_json") if isinstance(t.get("metadata_json"), dict) else {}
        pbi = meta.get("powerbi") if isinstance(meta.get("powerbi"), dict) else {}
        ssas = meta.get("analysis_services") if isinstance(meta.get("analysis_services"), dict) else {}
        model = str(pbi.get("datasetId") or ssas.get("catalog") or ssas.get("database") or "")
        internal = str(pbi.get("tableName") or ssas.get("tableName") or ssas.get("table")
                       or str(t["name"]).split("/", 1)[-1]).strip().lower()
        if internal:
            models.setdefault(model, {})[internal] = t
    return models


def _trace_dax(dax: str, catalog: Catalog, target: Optional[dict]) -> Lineage:
    out = Lineage()
    tokens = dax_tokens(dax)
    if not tokens:
        return out
    models = _dax_models(catalog)
    if not models:
        return out

    # Table names the text mentions: quoted identifiers, and bare words that
    # are not function calls.
    mentioned = set()
    for i, (kind, text) in enumerate(tokens):
        if kind == "quoted":
            mentioned.add(_table_name(kind, text).lower())
        elif kind == "word" and (i + 1 >= len(tokens) or tokens[i + 1][1] != "("):
            mentioned.add(text.lower())

    pinned = str((target or {}).get("datasetId") or "")
    if pinned:
        if pinned not in models:
            return out
        model = models[pinned]
    else:
        scored = sorted(((len(mentioned & set(m)), key) for key, m in models.items()), reverse=True)
        if not scored or scored[0][0] == 0:
            return out
        if len(scored) > 1 and scored[1][0] == scored[0][0]:
            return out          # two models fit equally: not ours to pick
        model = models[scored[0][1]]

    out.tables = [model[n]["name"] for n in sorted(mentioned & set(model))]
    used_tables = [model[n] for n in sorted(mentioned & set(model))]

    # Measures are unique within a model; index them, and plain columns per table.
    measures: dict = {}
    for t in model.values():
        for c in t.get("columns") or []:
            if isinstance(c, dict) and _is_measure(c):
                measures.setdefault(str(c.get("name") or "").lower(), []).append((t, c))

    def measure(name: str):
        hits = measures.get(name.lower()) or []
        return hits[0] if len(hits) == 1 else None

    def column(table_name: str, name: str):
        t = model.get(table_name.lower())
        if t is None:
            return None
        hit = Catalog.column(t, name)
        return (t, hit) if hit is not None and not _is_measure(hit) else None

    # Measures the query defines for itself: `DEFINE MEASURE T[Name] = expr`.
    local: dict = {}
    for i, (kind, text) in enumerate(tokens):
        if kind == "word" and text.lower() == "measure" and i + 3 < len(tokens) \
                and tokens[i + 2][0] == "bracket" and tokens[i + 3][1] == "=":
            end = i + 4
            depth = 0
            while end < len(tokens):
                k, t_ = tokens[end]
                if t_ in "({":
                    depth += 1
                elif t_ in ")}":
                    depth -= 1
                elif depth == 0 and k == "word" and t_.lower() in ("measure", "var", "evaluate", "column", "table"):
                    break
                end += 1
            local[_bracket_name(tokens[i + 2][1]).lower()] = tokens[i + 4:end]

    def refs_in(expr_tokens: list) -> list:
        found = []
        for j, (kind, text) in enumerate(expr_tokens):
            if kind != "bracket":
                continue
            prev = expr_tokens[j - 1] if j else ("", "")
            name = _bracket_name(text)
            hit = None
            if prev[0] in ("word", "quoted") and (j < 2 or expr_tokens[j - 2][1] != "."):
                hit = column(_table_name(*prev), name) or (
                    measure(name) if name.lower() not in local else None)
            elif name.lower() not in local:
                hit = measure(name)
            if hit is not None:
                ref = SourceRef(hit[0]["name"], hit[1]["name"])
                if ref not in found:
                    found.append(ref)
        return found

    def of_expression(expr_tokens: list) -> Optional[ColumnLineage]:
        if not expr_tokens:
            return None
        if len(expr_tokens) == 1 and expr_tokens[0][0] == "bracket":
            name = _bracket_name(expr_tokens[0][1])
            if name.lower() in local:
                body = local[name.lower()]
                return ColumnLineage(KIND_DERIVED, expression=_dax_text(body), reads=refs_in(body))
            hit = measure(name)
            if hit is None:
                return None
            return ColumnLineage(KIND_MEASURE, source=SourceRef(hit[0]["name"], hit[1]["name"]))
        if len(expr_tokens) == 2 and expr_tokens[0][0] in ("word", "quoted") and expr_tokens[1][0] == "bracket":
            name = _bracket_name(expr_tokens[1][1])
            hit = column(_table_name(*expr_tokens[0]), name)
            if hit is not None:
                return ColumnLineage(KIND_COLUMN, source=SourceRef(hit[0]["name"], hit[1]["name"]))
            if name.lower() not in local:
                m = measure(name)       # a measure may be written Table[Measure]
                if m is not None:
                    return ColumnLineage(KIND_MEASURE, source=SourceRef(m[0]["name"], m[1]["name"]))
            return None
        return ColumnLineage(KIND_DERIVED, expression=_dax_text(expr_tokens), reads=refs_in(expr_tokens))

    # `"Name", <expression>` pairs in the argument lists of the functions that
    # name their outputs.
    named: dict = {}
    grouped: list = []      # (table, column) written as bare group-by arguments
    for i, (kind, text) in enumerate(tokens):
        if kind != "word" or text.lower() not in _DAX_NAMING or i + 1 >= len(tokens) or tokens[i + 1][1] != "(":
            continue
        if text.lower() in _DAX_AGGREGATING:
            out.aggregated = True
        args = _dax_args(tokens, i + 1)
        j = 0
        while j < len(args):
            arg = args[j]
            if len(arg) == 1 and arg[0][0] == "string" and j + 1 < len(args):
                name = arg[0][1][1:-1].replace('""', '"')
                named.setdefault(name.lower(), []).append(of_expression(args[j + 1]))
                j += 2
                continue
            if len(arg) == 2 and arg[0][0] in ("word", "quoted") and arg[1][0] == "bracket":
                grouped.append((_table_name(*arg[0]).lower(), _bracket_name(arg[1][1]).lower()))
            j += 1

    # Every `Table[Column]` written anywhere, by column name.
    explicit: dict = {}
    for j, (kind, text) in enumerate(tokens):
        if kind == "bracket" and j and tokens[j - 1][0] in ("word", "quoted"):
            table_name, name = _table_name(*tokens[j - 1]), _bracket_name(text)
            hit = column(table_name, name)
            if hit is not None:
                explicit.setdefault(name.lower(), set()).add((hit[0]["name"], hit[1]["name"]))
                explicit.setdefault(f"{table_name}[{name}]".lower(), set()).add((hit[0]["name"], hit[1]["name"]))

    def lookup(output: str) -> Optional[ColumnLineage]:
        key = output.lower()
        if key in named:
            hits = named[key]
            return hits[0] if len(hits) == 1 else None
        refs = explicit.get(key)
        if refs is not None:
            if len(refs) != 1:
                return None
            table, col = next(iter(refs))
            lin = ColumnLineage(KIND_COLUMN, source=SourceRef(table, col))
            lin.grouping = out.aggregated
            return lin
        # `EVALUATE 'Table'`: a column of the one table read that has it.
        owners = [(t, Catalog.column(t, output)) for t in used_tables]
        owners = [(t, c) for t, c in owners if c is not None and not _is_measure(c)]
        if len(owners) == 1 and not named and not explicit:
            return ColumnLineage(KIND_COLUMN, source=SourceRef(owners[0][0]["name"], owners[0][1]["name"]))
        return None

    out.lookup = lookup
    return out


def _is_measure(column: dict) -> bool:
    meta = column.get("metadata") if isinstance(column.get("metadata"), dict) else {}
    return str(meta.get("role") or meta.get("kind") or "").lower() == "measure" \
        or str(column.get("dtype") or "").lower() == "measure"


def _dax_args(tokens: list, open_index: int) -> list:
    """The top-level arguments of the call whose `(` is at `open_index`."""
    args, current, depth = [], [], 0
    for kind, text in tokens[open_index + 1:]:
        if text in "({":
            depth += 1
        elif text in ")}":
            if depth == 0:
                break
            depth -= 1
        elif text == "," and depth == 0:
            args.append(current)
            current = []
            continue
        current.append((kind, text))
    if current:
        args.append(current)
    return args


# --------------------------------------------------------------------------
# Field lists: Priority OData, BusinessObjects
# --------------------------------------------------------------------------

def _trace_fields(definition: str, catalog: Catalog, conn_type: str) -> Lineage:
    out = Lineage()
    text = definition.strip()
    if conn_type == "businessobjects":
        try:
            doc = json.loads(text)
        except ValueError:
            return out
        if not isinstance(doc, dict):
            return out
        universe = str(doc.get("universe") or doc.get("universe_id") or "").strip().lower()
        table = None
        for t in catalog.tables:
            meta = (t.get("metadata_json") or {}).get("businessobjects") or {}
            if universe and universe in (str(t["name"]).lower(), str(meta.get("universe_name") or "").lower(),
                                         str(meta.get("universe_id") or "").lower()):
                if table is not None:
                    return out
                table = t
        if table is None:
            return out
        out.tables = [table["name"]]
        for obj in doc.get("objects") or []:
            hit = Catalog.column(table, str(obj))
            lin = None
            if hit is not None:
                kind = KIND_MEASURE if _is_measure(hit) else KIND_COLUMN
                lin = ColumnLineage(kind, source=SourceRef(table["name"], hit["name"]))
            out.ordered.append((str(obj), lin))
        out.positional = True
        out.aggregated = any(lin is not None and lin.kind == KIND_MEASURE for _, lin in out.ordered)
        return out

    # OData: FORM or FORM?$select=A,B&$filter=...
    form, _, query = text.lstrip("/").partition("?")
    table = catalog.table((form.strip(),)) if form.strip() else None
    if table is None:
        return out
    out.tables = [table["name"]]
    options = {k.lower(): v for k, v in parse_qs(query, keep_blank_values=True).items()}
    if "$apply" in options:
        return out
    selected = [f.strip() for f in ",".join(options.get("$select", [])).split(",") if f.strip()]
    if not selected:
        selected = [c["name"] for c in table.get("columns") or [] if isinstance(c, dict) and c.get("name")]
    for name in selected:
        hit = Catalog.column(table, name)
        out.ordered.append((name, ColumnLineage(
            KIND_COLUMN, source=SourceRef(table["name"], hit["name"])) if hit is not None else None))
    # An $expand adds columns whose names we do not predict: match by name.
    out.positional = "$expand" not in options
    return out
