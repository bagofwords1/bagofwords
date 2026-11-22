"""What a custom table's columns mean: inherited, classified, described.

A custom table used to be a name, column names and types. The catalog
already knows more about the columns it was built from — comments, measure
expressions, titles — and this module carries that across, with no call to
any source:

  * each column gets the description and metadata of the indexed column it
    was copied from (`lineage.py` says which), or its own expression when it
    is derived;
  * each column is marked additive / not additive / unknown
    (`additivity.py`), so nobody sums a distinct count;
  * a fingerprint of the source's expression is kept, so a later refresh can
    tell that a definition moved under the cached copy;
  * what an admin edited by hand wins over all of it, on every refresh.

Shape of a stored column:

    {"name": "customers", "dtype": "BIGINT",
     "description": "COUNT(DISTINCT o.customer_id). Reads public.orders.customer_id: Buyer",
     "metadata": {
        "role": "measure",
        "source": {"kind": "derived", "expression": "COUNT(DISTINCT o.customer_id)",
                   "reads": [{"table": "public.orders", "column": "customer_id"}]},
        "additive": false, "additive_reason": "distinct_count",
        "recompute": "SUM(revenue) / SUM(orders)",   # ratios only, when both parts are columns
        "fingerprint": "5f1c…",
        "edited": ["description"],              # only when an admin edited
        "inherited": {"description": "…"},      # what the edit replaced
        "drift": {"detected_at": "…", "previous_fingerprint": "…"}   # only when set
     }}
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.data_sources.fast import additivity, lineage as lineage_mod

DESCRIPTION_LIMIT = 500
EXPRESSION_LIMIT = 2000

# Keys this module owns inside a column's `metadata`. Everything else there
# was copied from the source column.
OWN_KEYS = ("source", "additive", "additive_reason", "fingerprint", "edited", "inherited", "drift",
            "recompute")
EDITABLE = ("description", "additive")
SET_BY_ADMIN = "set_by_admin"

_REASON_WORDS = {
    additivity.DISTINCT: "distinct count",
    additivity.AVERAGE: "average",
    additivity.RATIO: "ratio",
    additivity.MIN_MAX: "minimum / maximum",
    additivity.TIME: "time intelligence",
    additivity.TOTAL: "share of a total",
    additivity.STATISTIC: "statistic",
    additivity.OFFSET: "constant added after aggregation",
}
# Not additive, but nobody would think to sum them: no note for the agent.
_SILENT = {additivity.NOT_NUMERIC, additivity.GROUPING, additivity.KEY}


@dataclass
class Enriched:
    columns: list
    # One entry per column whose source definition changed since it was last
    # described: {"column", "source", "message"}.
    drift: list = field(default_factory=list)
    # Catalog tables the definition reads.
    sources: list = field(default_factory=list)
    # Those of them the indexer marked as carrying row-level security.
    rls_sources: list = field(default_factory=list)
    aggregated: bool = False


def _clip(text, limit: int) -> Optional[str]:
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _expression_of(column: Optional[dict]) -> str:
    meta = (column or {}).get("metadata") if isinstance((column or {}).get("metadata"), dict) else {}
    for key in ("expression", "formula", "definition", "calculation"):
        if meta.get(key):
            return str(meta[key])
    return ""


def fingerprint(*parts) -> str:
    text = "\x1f".join(re.sub(r"\s+", " ", str(p or "")).strip() for p in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _source_fingerprint(column: Optional[dict]) -> str:
    """Of one indexed column: its expression where it has one, else its type.
    The description is deliberately left out — a reworded comment follows on
    refresh, it is not a changed definition."""
    expression = _expression_of(column)
    return fingerprint("expr", expression) if expression else fingerprint("type", (column or {}).get("dtype"))


def _keys_of(table: dict) -> set:
    names = set()
    for pk in table.get("pks") or []:
        if isinstance(pk, dict) and pk.get("name"):
            names.add(str(pk["name"]).lower())
    for fk in table.get("fks") or []:
        col = fk.get("column") if isinstance(fk, dict) else None
        if isinstance(col, dict) and col.get("name"):
            names.add(str(col["name"]).lower())
    return names


def _same_source(a, b) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    keys = ("kind", "table", "column", "expression")
    return all(a.get(k) == b.get(k) for k in keys)


def clean_overrides(raw, column_names=None) -> dict:
    """The admin's per-column edits as `{name: {description?, additive?}}`.

    Accepts the API's list form (`[{"name", "description", "additive"}]`) or
    the stored dict form. Only the two editable fields survive, and only for
    columns that exist when `column_names` is given.
    """
    items = []
    if isinstance(raw, dict):
        items = [{"name": k, **v} for k, v in raw.items() if isinstance(v, dict)]
    elif isinstance(raw, list):
        items = [i for i in raw if isinstance(i, dict)]
    known = {str(n).lower() for n in column_names} if column_names is not None else None
    out: dict = {}
    for item in items:
        name = str(item.get("name") or "").strip()
        if not name or (known is not None and name.lower() not in known):
            continue
        edit = {}
        if isinstance(item.get("description"), str):
            edit["description"] = item["description"].strip()[:DESCRIPTION_LIMIT]
        if "additive" in item and item["additive"] in (True, False, additivity.UNKNOWN):
            edit["additive"] = item["additive"]
        if edit:
            out[name] = edit
    return out


def enrich(columns: list, found: Optional[lineage_mod.Lineage], tables: list, *,
           previous: Optional[list] = None, overrides: Optional[dict] = None,
           now: Optional[datetime] = None) -> Enriched:
    """`columns` (`[{"name", "dtype"}]`, as the load stored them) with
    everything the catalog knows about where each came from.

    `previous` is what the table's columns held before this refresh: it is
    where drift is measured from and where a still-unreviewed drift flag is
    carried over. `overrides` are the admin's edits.
    """
    found = found or lineage_mod.Lineage()
    by_name = {str(t.get("name")): t for t in tables or [] if isinstance(t, dict)}
    before = {str(c.get("name")).lower(): c for c in previous or [] if isinstance(c, dict)}
    edits = {k.lower(): v for k, v in clean_overrides(overrides or {}).items()}
    stamp = (now or datetime.utcnow()).isoformat(timespec="seconds")

    def indexed(ref) -> Optional[dict]:
        table = by_name.get(ref.table)
        return lineage_mod.Catalog.column(table, ref.column) if table else None

    measures_cache: dict = {}

    def measure_lookup(model_table: Optional[str]):
        """Expression of a measure by name, among the tables of one model."""
        if model_table not in measures_cache:
            index: dict = {}
            home = by_name.get(model_table or "")
            model = _model_of(home)
            for t in by_name.values():
                if _model_of(t) != model:
                    continue
                for c in t.get("columns") or []:
                    if isinstance(c, dict) and lineage_mod._is_measure(c):
                        index.setdefault(str(c.get("name") or "").lower(), _expression_of(c))
            measures_cache[model_table] = index
        index = measures_cache[model_table]
        return lambda name: index.get(str(name).lower()) or None

    out = Enriched(columns=[], sources=list(found.tables), aggregated=found.aggregated)
    traced: dict = {}       # output name -> its lineage, for the ratio pass below

    for col in columns or []:
        if not isinstance(col, dict) or not col.get("name"):
            continue
        name, dtype = str(col["name"]), col.get("dtype")
        lin = found.columns.get(name)
        description: Optional[str] = None
        meta: dict = {}
        verdict = additivity.Additivity(additivity.UNKNOWN, additivity.UNREADABLE)
        numeric = additivity.is_numeric(dtype)

        if lin is None:
            if not numeric:
                verdict = additivity.Additivity(False, additivity.NOT_NUMERIC)
        elif lin.kind in (lineage_mod.KIND_COLUMN, lineage_mod.KIND_MEASURE):
            src = indexed(lin.source) or {}
            description = _clip(src.get("description"), DESCRIPTION_LIMIT)
            if not description and lin.kind == lineage_mod.KIND_MEASURE:
                description = _clip(_expression_of(src), DESCRIPTION_LIMIT)
            if isinstance(src.get("metadata"), dict):
                meta.update({k: v for k, v in src["metadata"].items() if k not in OWN_KEYS})
            meta["source"] = {"kind": lin.kind, "table": lin.source.table, "column": lin.source.column}
            meta["fingerprint"] = _source_fingerprint(src)
            if lin.kind == lineage_mod.KIND_MEASURE:
                expression = _expression_of(src)
                if found.family == lineage_mod.FAMILY_DAX:
                    verdict = additivity.of_dax(expression, measure_lookup(lin.source.table))
                else:
                    verdict = additivity.Additivity(additivity.UNKNOWN, additivity.UNREADABLE)
            elif lin.grouping:
                verdict = additivity.Additivity(False, additivity.GROUPING)
                meta.setdefault("role", "dimension")
            elif lin.source.column.lower() in _keys_of(by_name.get(lin.source.table) or {}):
                verdict = additivity.Additivity(False, additivity.KEY)
            else:
                verdict = additivity.of_plain_column(dtype or src.get("dtype"))
        else:  # derived
            expression = _clip(lin.expression, EXPRESSION_LIMIT) or ""
            reads = [{"table": r.table, "column": r.column} for r in lin.reads]
            meta["source"] = {"kind": lineage_mod.KIND_DERIVED, "expression": expression, "reads": reads}
            read_columns = [(r, indexed(r) or {}) for r in lin.reads]
            meta["fingerprint"] = fingerprint(
                "derived", expression, *[_source_fingerprint(c) for _, c in read_columns])
            notes = [
                f"{r.table}.{r.column}: {str(c.get('description')).strip()}"
                for r, c in read_columns[:3]
                if c.get("description") and not lineage_mod._is_measure(c)
            ]
            description = _clip(expression + (". Reads " + "; ".join(notes) if notes else ""),
                                DESCRIPTION_LIMIT)
            if lin.grouping:
                verdict = additivity.Additivity(False, additivity.GROUPING)
                meta["role"] = "dimension"
            else:
                if found.family == lineage_mod.FAMILY_DAX:
                    home = lin.reads[0].table if lin.reads else (found.tables[0] if found.tables else None)
                    verdict = additivity.of_dax(lin.expression or "", measure_lookup(home))
                else:
                    verdict = additivity.of_sql(lin.node)
                if found.aggregated or found.family == lineage_mod.FAMILY_DAX:
                    meta["role"] = "measure"
                # A date or a label cannot be summed whatever computed it; a
                # reason the classifier already found (MAX of a date) is kept.
                if not numeric and dtype and verdict.additive is not False:
                    verdict = additivity.Additivity(False, additivity.NOT_NUMERIC)

        meta["additive"] = verdict.additive
        meta["additive_reason"] = verdict.reason

        # Drift: same source as last time, different definition behind it.
        prior = before.get(name.lower()) or {}
        prior_meta = prior.get("metadata") if isinstance(prior.get("metadata"), dict) else {}
        if _same_source(prior_meta.get("source"), meta.get("source")) \
                and prior_meta.get("fingerprint") and meta.get("fingerprint") \
                and prior_meta["fingerprint"] != meta["fingerprint"]:
            meta["drift"] = {"detected_at": stamp, "previous_fingerprint": prior_meta["fingerprint"]}
            # What it used to be, when the expression itself is what moved (a
            # measure rewritten at the source). When only a column it reads
            # changed, the text is the same and repeating it would mislead.
            was = _expression_of(prior)
            if was and was != _expression_of({"metadata": meta}):
                meta["drift"]["previous_expression"] = _clip(was, DESCRIPTION_LIMIT)
            out.drift.append({
                "column": name, "source": meta["source"],
                "message": f"Column {name}: the definition of {_source_label(meta['source'])} "
                           f"changed at the source since this table last described it.",
            })
        elif isinstance(prior_meta.get("drift"), dict) and _same_source(
                prior_meta.get("source"), meta.get("source")):
            meta["drift"] = prior_meta["drift"]      # not reviewed yet

        # The admin's edits win, and say so.
        edit = edits.get(name.lower()) or {}
        edited, inherited = [], {}
        if "description" in edit:
            inherited["description"] = description
            description = edit["description"] or None
            edited.append("description")
        if "additive" in edit:
            inherited["additive"] = meta["additive"]
            inherited["additive_reason"] = meta["additive_reason"]
            meta["additive"], meta["additive_reason"] = edit["additive"], SET_BY_ADMIN
            edited.append("additive")
        if edited:
            meta["edited"], meta["inherited"] = edited, inherited

        for ref in ([lin.source] if lin is not None and lin.source is not None else []) + \
                (list(lin.reads) if lin is not None else []):
            if ref.table not in out.sources:
                out.sources.append(ref.table)

        traced[name] = lin
        entry = {"name": name, "dtype": dtype}
        if description:
            entry["description"] = description
        entry["metadata"] = meta
        out.columns.append(entry)
    out.rls_sources = [name for name in out.sources if _has_rls(by_name.get(name))]
    _add_recomputations(out.columns, traced, found)
    return out


def _quoted(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else '"' + name.replace('"', '""') + '"'


def _add_recomputations(columns: list, traced: dict, found: lineage_mod.Lineage) -> None:
    """For a ratio whose numerator and denominator are themselves additive
    columns of this table, record how to get it right at a coarser grain:
    `SUM(numerator) / SUM(denominator)`, in the SQL the cached copy is
    queried with. Only when both parts are found exactly; never approximated.
    """
    additive = {
        c["name"] for c in columns
        if c["metadata"].get("additive") is True and "edited" not in c["metadata"]
    }

    def part_of(matches) -> Optional[str]:
        hits = [name for name in additive if traced.get(name) is not None and matches(traced[name])]
        return hits[0] if len(hits) == 1 else None

    for col in columns:
        meta, lin = col["metadata"], traced.get(col["name"])
        if lin is None or meta.get("additive") is not False:
            continue
        if "edited" in meta and "additive" in meta["edited"]:
            continue
        if meta.get("additive_reason") == additivity.MIN_MAX:
            # The smallest of the monthly minimums is the yearly minimum: a
            # direct MIN or MAX rolls up with the same function.
            extremum = _dax_extremum(_expression_text(lin, columns, col)) \
                if found.family == lineage_mod.FAMILY_DAX else _sql_extremum(lin.node)
            if extremum:
                meta["recompute"] = f"{extremum}({_quoted(col['name'])})"
            continue
        if meta.get("additive_reason") != additivity.RATIO:
            continue
        numerator = denominator = None
        if found.family == lineage_mod.FAMILY_DAX:
            parts = _dax_divide(_expression_text(lin, columns, col))
            if parts:
                numerator, denominator = (part_of(_dax_matcher(p)) for p in parts)
        elif lin.node is not None:
            parts = _sql_divide(lin.node)
            if parts:
                numerator, denominator = (part_of(_sql_matcher(p, found.dialect)) for p in parts)
        if numerator and denominator and numerator != denominator:
            meta["recompute"] = f"SUM({_quoted(numerator)}) / SUM({_quoted(denominator)})"


def _expression_text(lin, columns, col) -> str:
    if lin.kind == lineage_mod.KIND_DERIVED:
        return lin.expression or ""
    return str(col["metadata"].get("expression") or "")


def _sql_divide(node):
    from sqlglot import exp

    def strip(n):
        while True:
            if isinstance(n, (exp.Paren, exp.Cast, exp.TryCast)):
                n = n.this
            elif isinstance(n, exp.Nullif) and isinstance(n.expression, exp.Literal):
                n = n.this
            else:
                return n

    node = strip(node)
    if not isinstance(node, exp.Div):
        return None
    return strip(node.this), strip(node.expression)


def _sql_matcher(part, dialect):
    text = part.sql(dialect=dialect).lower()

    def matches(lin) -> bool:
        if lin.kind == lineage_mod.KIND_DERIVED and lin.node is not None:
            return lin.node.sql(dialect=dialect).lower() == text
        return False
    return matches


def _sql_extremum(node) -> Optional[str]:
    """"MIN" / "MAX" when the expression is exactly that aggregate (through
    casts), else None."""
    from sqlglot import exp

    while isinstance(node, (exp.Paren, exp.Cast, exp.TryCast)):
        node = node.this
    if isinstance(node, exp.Min):
        return "MIN"
    if isinstance(node, exp.Max):
        return "MAX"
    return None


def _dax_extremum(expression: str) -> Optional[str]:
    tokens = lineage_mod.dax_tokens(expression)
    if len(tokens) < 4 or tokens[1][1] != "(" or additivity._closes(tokens, 1) != len(tokens) - 1:
        return None
    name = tokens[0][1].lower()
    return "MIN" if name == "min" else "MAX" if name == "max" else None


def _dax_divide(expression: str):
    tokens = lineage_mod.dax_tokens(expression)
    if len(tokens) < 6 or tokens[0][1].lower() != "divide" or tokens[1][1] != "(":
        return None
    # The whole expression must be the one DIVIDE call. `DIVIDE(a, b) * 100`
    # is a percentage: SUM(a) / SUM(b) would be off by the factor.
    if additivity._closes(tokens, 1) != len(tokens) - 1:
        return None
    args = lineage_mod._dax_args(tokens, 1)
    if len(args) < 2:
        return None
    return args[0], args[1]


def _dax_matcher(arg_tokens):
    # A bare `[Measure]` reference: matches the column that is that measure.
    name = arg_tokens[0][1][1:-1].replace("]]", "]").strip().lower() \
        if len(arg_tokens) == 1 and arg_tokens[0][0] == "bracket" else None

    def matches(lin) -> bool:
        return bool(name) and lin.kind == lineage_mod.KIND_MEASURE and lin.source.column.lower() == name
    return matches


def _model_of(table: Optional[dict]) -> str:
    meta = (table or {}).get("metadata_json") if isinstance((table or {}).get("metadata_json"), dict) else {}
    for key in ("powerbi", "analysis_services"):
        inner = meta.get(key) if isinstance(meta.get(key), dict) else {}
        for k in ("datasetId", "catalog", "database"):
            if inner.get(k):
                return str(inner[k])
    return ""


def _has_rls(table: Optional[dict]) -> bool:
    meta = (table or {}).get("metadata_json") if isinstance((table or {}).get("metadata_json"), dict) else {}
    if meta.get("rowLevelSecurity"):
        return True
    return any(isinstance(v, dict) and v.get("rowLevelSecurity") for v in meta.values())


def _source_label(source: dict) -> str:
    if source.get("kind") == lineage_mod.KIND_DERIVED:
        reads = source.get("reads") or []
        return ", ".join(f"{r['table']}.{r['column']}" for r in reads[:3]) or "its expression"
    return f"{source.get('table')}.{source.get('column')}"


def acknowledge_drift(columns: list, names) -> list:
    """`columns` with the drift flag cleared on the named ones."""
    wanted = {str(n).lower() for n in names or []}
    out = []
    for col in columns or []:
        meta = col.get("metadata") if isinstance(col, dict) else None
        if isinstance(meta, dict) and "drift" in meta and str(col.get("name")).lower() in wanted:
            col = {**col, "metadata": {k: v for k, v in meta.items() if k != "drift"}}
        out.append(col)
    return out


# --------------------------------------------------------------------------
# What the agent reads
# --------------------------------------------------------------------------

def agent_note(metadata, dtype=None) -> Optional[str]:
    """The short re-aggregation note for one column, or None.

    Said only where it changes what a correct query looks like: a numeric
    column that must not be summed, or a computed one nobody could classify.
    """
    if not isinstance(metadata, dict) or "additive" not in metadata:
        return None
    value, reason = metadata.get("additive"), metadata.get("additive_reason")
    if value is False:
        if reason in _SILENT:
            return None
        words = _REASON_WORDS.get(reason)
        head = "NOT ADDITIVE" + (f" ({words})" if words else "")
        recompute = metadata.get("recompute")
        if recompute:
            return (f"{head}: read it only row by row. Never SUM or AVG it. Over several rows "
                    f"compute {recompute} instead")
        return (f"{head}: each row's value stands alone. SUM or AVG over several rows is wrong, "
                "also when a 'total' is asked for. For a coarser grain do not derive it from this "
                "column: query the source tables if you have them, otherwise say this table "
                "cannot answer it")
    if value == additivity.UNKNOWN:
        source = metadata.get("source") if isinstance(metadata.get("source"), dict) else {}
        if source.get("kind") in (lineage_mod.KIND_DERIVED, lineage_mod.KIND_MEASURE):
            return ("additivity unknown: it is not established that this column may be summed; "
                    "check how it is computed before aggregating it over several rows")
    return None


def additivity_label(metadata) -> Optional[str]:
    """`additive`, `not additive (ratio)` or `unknown`, for descriptions."""
    if not isinstance(metadata, dict) or "additive" not in metadata:
        return None
    value, reason = metadata.get("additive"), metadata.get("additive_reason")
    if value is True:
        return "additive"
    if value is False:
        words = _REASON_WORDS.get(reason)
        return "not additive" + (f": {words}" if words else "")
    return "additivity unknown"


def schedule_text(mode, interval_minutes, at_time) -> Optional[str]:
    if mode == "time" and at_time:
        return f"daily at {at_time}"
    if interval_minutes:
        minutes = int(interval_minutes)
        if minutes % 1440 == 0:
            n, unit = minutes // 1440, "day"
        elif minutes % 60 == 0:
            n, unit = minutes // 60, "hour"
        else:
            n, unit = minutes, "minute"
        return f"every {unit}" if n == 1 else f"every {n} {unit}s"
    return None


def describe_table(columns: list, *, sources: Optional[list] = None, connection_name: Optional[str] = None,
                   schedule: Optional[str] = None, aggregated: bool = False) -> Optional[str]:
    """A description of the table made of facts only: its grain, its measures
    and whether each may be summed, where it reads from, how often.

    Built by code from the enriched columns; no model is asked. Returns None
    when there is nothing to say beyond the column list itself.
    """
    grain, measures, plain = [], [], []
    for col in columns or []:
        meta = col.get("metadata") if isinstance(col.get("metadata"), dict) else {}
        reason = meta.get("additive_reason")
        role = str(meta.get("role") or "").lower()
        source = meta.get("source") if isinstance(meta.get("source"), dict) else {}
        is_measure = (
            role == "measure" or source.get("kind") == lineage_mod.KIND_MEASURE
            or (source.get("kind") == lineage_mod.KIND_DERIVED and aggregated and reason not in _SILENT)
        )
        if reason == additivity.GROUPING or (aggregated and role == "dimension"):
            grain.append(str(col["name"]))
        elif is_measure or (aggregated and source.get("kind") == lineage_mod.KIND_DERIVED):
            label = additivity_label(meta)
            measures.append(f"{col['name']} ({label})" if label else str(col["name"]))
        else:
            plain.append(str(col["name"]))

    parts = []
    if aggregated and grain:
        parts.append("One row per " + ", ".join(grain) + ".")
    elif not aggregated and sources and len(sources) == 1 and not measures:
        parts.append(f"Row-level copy of {sources[0]} ({len(columns or [])} columns).")
    elif not aggregated and sources:
        parts.append("Row-level result.")
    if measures:
        parts.append("Measures: " + ", ".join(measures) + ".")
    if sources:
        where = ", ".join(sources[:6]) + (f" and {len(sources) - 6} more" if len(sources) > 6 else "")
        parts.append(f"Source: {where}" + (f" on {connection_name}" if connection_name else "") + ".")
    if not parts:
        return None
    if schedule:
        parts.append(f"Refreshed {schedule}.")
    return " ".join(parts)


def to_json(value) -> str:
    """Stable JSON of stored columns, for reports and logs."""
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False)
