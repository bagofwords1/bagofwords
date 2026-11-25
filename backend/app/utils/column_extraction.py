"""Which catalog columns an executed query actually references.

Two extractors, both pure (no DB, no network):

* :func:`extract_sql_columns` parses SQL with sqlglot and resolves every column
  reference (SELECT, WHERE, JOIN ... ON / USING, GROUP BY, HAVING, ORDER BY,
  window clauses, CTEs, subqueries, correlated subqueries) to the physical
  table it reads, using the known schema for unprefixed names. ``SELECT *``
  does not count as using every column.
* :func:`extract_dsl_columns` walks an Elasticsearch/OpenSearch query envelope
  (``query``, ``aggs``, ``sort``, ``_source``, ``fields``, ``runtime_mappings``
  scripts, or an embedded ``sql``) and maps each field path back to a catalog
  column (``x.keyword`` -> ``x``, ``items.sku`` -> ``items[].sku``).

A column is only reported when it resolves to a column of a catalog table, so
aliases, typos and computed names never become "usage".

The catalog is a list of :class:`CatalogTable` the caller loaded for the
agent; results are keyed by ``CatalogTable.key`` (the ``DataSourceTable`` id).
"""
from __future__ import annotations

import fnmatch
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# Queries longer than this are not parsed for column usage (stats are a
# best-effort signal; a multi-megabyte generated statement is not worth it).
MAX_QUERY_CHARS = 200_000


@dataclass
class CatalogTable:
    key: str                      # DataSourceTable id
    name: str                     # catalog name, e.g. "public.orders" or "logs-*"
    columns: List[str]            # column names as the catalog stores them
    _by_lower: Dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_lower = {}
        for c in self.columns or []:
            if c:
                self._by_lower.setdefault(str(c).lower(), str(c))

    def column(self, name: str) -> Optional[str]:
        """The catalog spelling of `name` (case-insensitive), or None."""
        if not name:
            return None
        return self._by_lower.get(str(name).lower())

    @property
    def name_parts(self) -> tuple:
        return tuple(p.strip('"`[]').lower() for p in self.name.split(".") if p)


def _match_table(parts: tuple, catalog: List[CatalogTable]) -> Optional[CatalogTable]:
    """Catalog table a (lowercased) dotted reference names. The shorter of the
    two names must be a trailing part of the longer (`orders` matches
    `public.orders`; `db.public.orders` matches `public.orders`). Ambiguous
    references (two catalog tables end with the same name) resolve to none."""
    if not parts:
        return None
    hits = []
    for t in catalog:
        tp = t.name_parts
        n = min(len(tp), len(parts))
        if n and tp[-n:] == parts[-n:]:
            hits.append(t)
    if len(hits) == 1:
        return hits[0]
    exact = [t for t in hits if t.name_parts == parts]
    return exact[0] if len(exact) == 1 else None


# --------------------------------------------------------------------- SQL
def sql_table_references(sql: str, dialect: Optional[str]) -> List[tuple]:
    """Dotted (lowercased) names of every physical table a statement reads.
    Empty when it cannot be parsed."""
    try:
        import sqlglot
        from sqlglot import exp

        trees = sqlglot.parse(sql, read=dialect) if sql and len(sql) <= MAX_QUERY_CHARS else []
    except Exception:
        return []
    out = []
    for tree in trees:
        if tree is None:
            continue
        cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        for t in tree.find_all(exp.Table):
            parts = tuple(p.lower() for p in (t.catalog, t.db, t.name) if p)
            if len(parts) == 1 and parts[0] in cte_names:
                continue
            if parts:
                out.append(parts)
    return out


def extract_sql_columns(sql: str, dialect: Optional[str], catalog: List[CatalogTable]) -> Dict[str, Set[str]]:
    """Map catalog table key -> set of catalog column names the SQL references."""
    result: Dict[str, Set[str]] = {}
    if not sql or not catalog or len(sql) > MAX_QUERY_CHARS:
        return result
    try:
        import sqlglot
        from sqlglot import exp
        from sqlglot.optimizer.scope import traverse_scope
    except Exception:  # pragma: no cover - sqlglot is a hard dependency
        return result
    try:
        trees = sqlglot.parse(sql, read=dialect)
    except Exception:
        return result

    def add(t: CatalogTable, col: Optional[str]) -> None:
        real = t.column(col) if col else None
        if real:
            result.setdefault(t.key, set()).add(real)

    for tree in trees:
        if tree is None:
            continue
        try:
            scopes = list(traverse_scope(tree))
        except Exception:
            scopes = []
        if not scopes:
            continue
        # Physical sources per scope: alias (lowercased) -> CatalogTable.
        phys: Dict[int, Dict[str, CatalogTable]] = {}
        for scope in scopes:
            m: Dict[str, CatalogTable] = {}
            for alias, source in scope.sources.items():
                if isinstance(source, exp.Table):
                    parts = tuple(p.lower() for p in (source.catalog, source.db, source.name) if p)
                    t = _match_table(parts, catalog)
                    if t is not None:
                        m[str(alias).lower()] = t
            phys[id(scope)] = m

        def resolve(scope, qualifier: str, name: str) -> List[CatalogTable]:
            """Tables a column reference reads, searching enclosing scopes for
            correlated references."""
            s = scope
            while s is not None:
                m = phys.get(id(s), {})
                if qualifier:
                    if qualifier in m:
                        return [m[qualifier]]
                    if qualifier in {str(a).lower() for a in s.sources}:
                        return []  # a derived source (CTE / subquery): counted inside it
                else:
                    owners = [t for t in m.values() if t.column(name)]
                    if len(owners) == 1:
                        return owners
                    if len(owners) > 1:
                        return []  # ambiguous: the database would refuse it
                    # A name a derived source projects is not a physical read here.
                    if any(not isinstance(src, exp.Table) for src in s.sources.values()):
                        derived_cols = set()
                        for src in s.sources.values():
                            sel = getattr(src, "expression", None)
                            if isinstance(sel, exp.Select):
                                derived_cols.update(p.alias_or_name.lower() for p in sel.expressions)
                        if name.lower() in derived_cols:
                            return []
                s = getattr(s, "parent", None)
            return []

        for scope in scopes:
            for col in scope.columns:
                if isinstance(col.this, exp.Star):
                    continue
                name = col.name
                if not name:
                    continue
                qualifier = (col.table or "").lower()
                for t in resolve(scope, qualifier, name):
                    add(t, name)
            # JOIN ... USING (a, b): identifiers, not Column nodes.
            select = scope.expression
            for join in (select.args.get("joins") or []) if isinstance(select, exp.Select) else []:
                for ident in join.args.get("using") or []:
                    name = getattr(ident, "name", None) or str(ident)
                    for t in phys.get(id(scope), {}).values():
                        add(t, name)
    return result


# --------------------------------------------------------------- Elastic DSL
# Leaf queries whose dict keys are field names: {"term": {"<field>": ...}}.
_FIELD_KEYED_QUERIES = {
    "term", "terms", "match", "match_phrase", "match_phrase_prefix", "match_bool_prefix",
    "range", "prefix", "wildcard", "regexp", "fuzzy", "terms_set", "intervals",
    "geo_distance", "geo_bounding_box", "geo_polygon", "geo_shape", "span_term",
}
_QUERY_PARAM_KEYS = {"boost", "_name", "minimum_should_match", "validation_method", "ignore_unmapped",
                     "distance", "distance_type", "type", "relation"}
_DOC_FIELD = re.compile(r"""doc\s*\[\s*['"]([^'"]+)['"]\s*\]""")
_SOURCE_FIELD = re.compile(r"""params\s*\.\s*_source\s*\.\s*([A-Za-z_@][\w.@]*)""")


def _strip_boost(f: str) -> str:
    return f.split("^", 1)[0].strip()


def dsl_field_references(envelope: Dict[str, Any]) -> Set[str]:
    """Every field path an Elasticsearch query envelope names."""
    fields: Set[str] = set()

    def from_query(q: Any) -> None:
        if isinstance(q, list):
            for item in q:
                from_query(item)
            return
        if not isinstance(q, dict):
            return
        for kind, body in q.items():
            if kind in _FIELD_KEYED_QUERIES and isinstance(body, dict):
                for k in body:
                    if k not in _QUERY_PARAM_KEYS:
                        fields.add(k)
            elif kind == "exists" and isinstance(body, dict) and isinstance(body.get("field"), str):
                fields.add(body["field"])
            elif kind in ("multi_match", "query_string", "simple_query_string", "combined_fields") and isinstance(body, dict):
                for f in body.get("fields") or []:
                    if isinstance(f, str) and "*" not in f:
                        fields.add(_strip_boost(f))
                if isinstance(body.get("default_field"), str) and "*" not in body["default_field"]:
                    fields.add(body["default_field"])
            elif kind == "nested" and isinstance(body, dict):
                if isinstance(body.get("path"), str):
                    fields.add(body["path"])
                from_query(body.get("query"))
            elif kind == "script" and isinstance(body, dict):
                from_script(body.get("script"))
            elif isinstance(body, (dict, list)):
                # bool/must/should/filter/must_not, constant_score, function_score, dis_max...
                from_query(body)

    def from_script(s: Any) -> None:
        src = s.get("source") if isinstance(s, dict) else s
        if isinstance(src, str):
            fields.update(_DOC_FIELD.findall(src))
            fields.update(_SOURCE_FIELD.findall(src))

    def from_aggs(aggs: Any) -> None:
        if not isinstance(aggs, dict):
            return
        for _name, body in aggs.items():
            if not isinstance(body, dict):
                continue
            for kind, spec in body.items():
                if kind in ("aggs", "aggregations"):
                    from_aggs(spec)
                elif kind == "filter":
                    from_query(spec)
                elif kind == "filters" and isinstance(spec, dict):
                    for q in (spec.get("filters") or {}).values() if isinstance(spec.get("filters"), dict) else spec.get("filters") or []:
                        from_query(q)
                elif isinstance(spec, dict):
                    generic(spec)

    def generic(node: Any) -> None:
        """Any "field" / "fields" / "sort" / "script" below an aggregation spec
        (terms, date_histogram, composite sources, top_hits, ...)."""
        if isinstance(node, list):
            for n in node:
                generic(n)
            return
        if not isinstance(node, dict):
            return
        for k, v in node.items():
            if k == "field" and isinstance(v, str):
                fields.add(v)
            elif k == "fields" and isinstance(v, list):
                for f in v:
                    if isinstance(f, str) and "*" not in f:
                        fields.add(_strip_boost(f))
                    elif isinstance(f, dict) and isinstance(f.get("field"), str):
                        fields.add(f["field"])
            elif k == "script":
                from_script(v)
            elif k == "sort":
                from_sort(v)
            elif k == "_source":
                from_source(v)
            elif isinstance(v, (dict, list)):
                generic(v)

    def from_sort(s: Any) -> None:
        items = s if isinstance(s, list) else [s]
        for item in items:
            if isinstance(item, str) and not item.startswith("_"):
                fields.add(item.split(":", 1)[0])
            elif isinstance(item, dict):
                for k, v in item.items():
                    if k == "_script" and isinstance(v, dict):
                        from_script(v.get("script"))
                    elif not k.startswith("_"):
                        fields.add(k)

    def from_source(s: Any) -> None:
        if isinstance(s, str) and "*" not in s:
            fields.add(s)
        elif isinstance(s, list):
            for f in s:
                if isinstance(f, str) and "*" not in f:
                    fields.add(f)
        elif isinstance(s, dict):
            from_source(s.get("includes") or s.get("include"))

    from_query(envelope.get("query"))
    from_query(envelope.get("post_filter"))
    from_aggs(envelope.get("aggs") or envelope.get("aggregations"))
    if "sort" in envelope:
        from_sort(envelope.get("sort"))
    if "_source" in envelope:
        from_source(envelope.get("_source"))
    for key in ("fields", "docvalue_fields", "stored_fields"):
        for f in envelope.get(key) or []:
            if isinstance(f, str) and "*" not in f:
                fields.add(f)
            elif isinstance(f, dict) and isinstance(f.get("field"), str):
                fields.add(f["field"])
    if isinstance(envelope.get("collapse"), dict) and isinstance(envelope["collapse"].get("field"), str):
        fields.add(envelope["collapse"]["field"])
    runtime = envelope.get("runtime_mappings")
    runtime_names = set(runtime) if isinstance(runtime, dict) else set()
    if isinstance(runtime, dict):
        for spec in runtime.values():
            if isinstance(spec, dict):
                from_script(spec.get("script"))
    # A runtime field is defined by the query, not mapped: not a catalog column.
    return {f for f in fields if f and f not in runtime_names}


def map_dsl_field(table: CatalogTable, path: str) -> Optional[str]:
    """Catalog column a DSL field path names.

    * exact match, except that a multi-field (`x.keyword`) folds into its
      parent field `x` when `x` is a column: usage belongs to the field, not to
      the index variant used to aggregate it;
    * nested arrays: `items.sku` -> `items[].sku` (how the catalog flattens a
      `nested` mapping).
    """
    if not path:
        return None
    base, _, sub = path.rpartition(".")
    if base and sub in ("keyword", "raw", "text", "lowercase", "normalized") and table.column(base):
        return table.column(base)
    exact = table.column(path)
    if exact:
        return exact
    parts = path.split(".")
    for i in range(1, len(parts)):
        candidate = ".".join(parts[:i]) + "[]." + ".".join(parts[i:])
        hit = table.column(candidate)
        if hit:
            return hit
    return None


def _index_tables(index_expr: str, catalog: List[CatalogTable]) -> List[CatalogTable]:
    out: List[CatalogTable] = []
    for token in (index_expr or "").split(","):
        token = token.strip()
        if not token or token.startswith("-"):
            continue
        for t in catalog:
            if t.name == token or fnmatch.fnmatchcase(t.name, token) or fnmatch.fnmatchcase(token, t.name):
                if t not in out:
                    out.append(t)
    return out


def extract_dsl_columns(envelope: Dict[str, Any], catalog: List[CatalogTable]) -> Dict[str, Set[str]]:
    """Map catalog table key -> catalog columns an ES/OpenSearch envelope uses."""
    result: Dict[str, Set[str]] = {}
    if not isinstance(envelope, dict) or not catalog:
        return result
    if isinstance(envelope.get("sql"), str):
        # Elasticsearch SQL names the index as a (quoted) table.
        return extract_sql_columns(envelope["sql"], None, catalog)
    tables = _index_tables(envelope.get("index") if isinstance(envelope.get("index"), str) else "", catalog)
    if not tables:
        return result
    body = envelope.get("body", envelope)
    if not isinstance(body, dict):
        return {}
    for path in dsl_field_references(body):
        for t in tables:
            col = map_dsl_field(t, path)
            if col:
                result.setdefault(t.key, set()).add(col)
    return result


def parse_json_envelope(query: Any) -> Optional[Dict[str, Any]]:
    """A query that is a JSON object (search DSL envelope), else None."""
    if isinstance(query, dict):
        return query
    if not isinstance(query, str):
        return None
    s = query.strip()
    if not s.startswith("{"):
        return None
    try:
        obj = json.loads(s)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def extract_columns(query: Any, dialect: Optional[str], catalog: List[CatalogTable]) -> Dict[str, Set[str]]:
    """Dispatch: JSON envelopes go to the DSL walker, text to the SQL parser."""
    env = parse_json_envelope(query)
    if env is not None:
        return extract_dsl_columns(env, catalog)
    if isinstance(query, str):
        return extract_sql_columns(query, dialect, catalog)
    return {}


def merge_into(target: Dict[str, Set[str]], more: Dict[str, Iterable[str]]) -> Dict[str, Set[str]]:
    for k, cols in (more or {}).items():
        target.setdefault(k, set()).update(cols)
    return target
