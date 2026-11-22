"""Whether a column's values may be re-aggregated by adding them up.

A custom table stores results at one grain. Rolling them up to a coarser one
is only right for some columns: a sum of sums is the sum, but a sum of
distinct counts, averages or ratios is a confident wrong number. This module
reads an expression and says which kind it is.

Three answers, never a guess:

  * ``True``      SUM, COUNT, and plain arithmetic over those;
  * ``False``     COUNT(DISTINCT), averages, ratios, MIN / MAX, time
                  intelligence — with the reason;
  * ``UNKNOWN``   the expression is unreadable or more than this classifier
                  understands. An admin sets those by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

UNKNOWN = "unknown"

# Reasons. Stored on the column and shown to the agent and the admin.
SUM = "sum"
COUNT = "count"
NUMERIC = "numeric_column"
DISTINCT = "distinct_count"
AVERAGE = "average"
RATIO = "ratio"
MIN_MAX = "min_max"
TIME = "time_intelligence"
TOTAL = "share_of_total"
STATISTIC = "statistic"
UNREADABLE = "unreadable"
COMPLEX = "complex"
NOT_NUMERIC = "not_numeric"
GROUPING = "grouping_key"
KEY = "key"


@dataclass(frozen=True)
class Additivity:
    additive: object          # True | False | "unknown"
    reason: str


def _yes(reason): return Additivity(True, reason)
def _no(reason): return Additivity(False, reason)
def _unknown(reason): return Additivity(UNKNOWN, reason)


_NUMERIC_TYPE = re.compile(
    r"int|decimal|numeric|number|double|float|real|money|fixed|edm\.(?:int|decimal|double|single|byte)|^long$",
    re.IGNORECASE,
)


def is_numeric(dtype) -> bool:
    text = str(dtype or "")
    return bool(_NUMERIC_TYPE.search(text)) and "interval" not in text.lower()


def of_plain_column(dtype) -> Additivity:
    """A stored value copied as is: additive when it is a number."""
    return _yes(NUMERIC) if is_numeric(dtype) else _no(NOT_NUMERIC)


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------

def of_sql(node) -> Additivity:
    """Classify a parsed sqlglot expression."""
    from sqlglot import exp

    if node is None:
        return _unknown(UNREADABLE)

    statistics = tuple(
        t for t in (
            getattr(exp, n, None) for n in (
                "Stddev", "StddevPop", "StddevSamp", "Variance", "VariancePop",
                "Quantile", "ApproxQuantile", "PercentileCont", "PercentileDisc",
                "Corr", "CovarPop", "CovarSamp", "Mode",
            )
        ) if t is not None
    )
    passthrough = tuple(
        t for t in (getattr(exp, n, None) for n in ("Paren", "Cast", "TryCast", "Round", "Neg", "Abs"))
        if t is not None
    )

    # What rules a whole expression out, wherever it appears in it.
    for found in node.walk():
        found = found[0] if isinstance(found, tuple) else found
        if isinstance(found, exp.Window):
            return _unknown(COMPLEX)
        if isinstance(found, exp.Count) and (found.args.get("distinct") or isinstance(found.this, exp.Distinct)):
            return _no(DISTINCT)
        if isinstance(found, (exp.ApproxDistinct, getattr(exp, "Hll", exp.ApproxDistinct))):
            return _no(DISTINCT)
        if isinstance(found, (exp.Avg, getattr(exp, "Median", exp.Avg))):
            return _no(AVERAGE)
        if statistics and isinstance(found, statistics):
            return _no(STATISTIC)
        if isinstance(found, (exp.Div, getattr(exp, "IntDiv", exp.Div), exp.Mod)):
            return _no(RATIO)
        if isinstance(found, (exp.Min, exp.Max)):
            return _no(MIN_MAX)
        if isinstance(found, exp.Anonymous) and str(found.name).lower() in (
                "safe_divide", "div0", "div0null", "divide"):
            return _no(RATIO)
        if isinstance(found, exp.Anonymous) and str(found.name).lower() in (
                "uniq", "uniqexact", "approx_count_distinct", "count_distinct", "ndv"):
            return _no(DISTINCT)

    def walk(n) -> Optional[str]:
        """The reason an expression is additive, or None when it cannot be told."""
        while isinstance(n, passthrough):
            n = n.this
        if isinstance(n, exp.Sum):
            return SUM
        if isinstance(n, (exp.Count, getattr(exp, "CountIf", exp.Count))):
            return COUNT
        if isinstance(n, (exp.Add, exp.Sub)):
            left, right = walk(n.this), walk(n.expression)
            return (left if left == right else SUM) if left and right else None
        if isinstance(n, exp.Mul):
            sides = [n.this, n.expression]
            constants = [s for s in sides if isinstance(s, exp.Literal) and not s.is_string]
            if len(constants) == 1:
                return walk(next(s for s in sides if s is not constants[0]))
            # Two row-level values multiplied (price * quantity) add up.
            if not n.find(exp.AggFunc):
                return NUMERIC
            return None
        if isinstance(n, exp.Coalesce):
            rest = n.expressions
            if all(isinstance(r, exp.Literal) and not r.is_string for r in rest):
                return walk(n.this)
            return None
        if isinstance(n, exp.Column):
            return NUMERIC
        if isinstance(n, exp.Literal) and not n.is_string:
            return NUMERIC
        return None

    reason = walk(node)
    return _yes(reason) if reason else _unknown(COMPLEX)


# --------------------------------------------------------------------------
# DAX
# --------------------------------------------------------------------------

_DAX_DISTINCT = {"distinctcount", "distinctcountnoblank", "approximatedistinctcount"}
_DAX_SET = {"distinct", "values"}
_DAX_AVERAGE = {"average", "averagea", "averagex", "median", "medianx", "geomean", "geomeanx"}
_DAX_STATISTIC = {
    "percentile.inc", "percentile.exc", "percentilex.inc", "percentilex.exc",
    "stdev.s", "stdev.p", "stdevx.s", "stdevx.p", "var.s", "var.p", "varx.s", "varx.p",
    "rankx", "rank", "topn",
}
_DAX_RATIO = {"divide", "quotient", "mod"}
_DAX_MIN_MAX = {"min", "max", "mina", "maxa", "minx", "maxx", "firstnonblank", "lastnonblank",
                "firstnonblankvalue", "lastnonblankvalue", "selectedvalue"}
_DAX_TIME = {
    "totalytd", "totalqtd", "totalmtd", "datesytd", "datesqtd", "datesmtd", "sameperiodlastyear",
    "dateadd", "parallelperiod", "previousday", "previousmonth", "previousquarter", "previousyear",
    "nextday", "nextmonth", "nextquarter", "nextyear", "datesbetween", "datesinperiod",
    "closingbalancemonth", "closingbalancequarter", "closingbalanceyear",
    "openingbalancemonth", "openingbalancequarter", "openingbalanceyear",
    "firstdate", "lastdate", "startofmonth", "startofquarter", "startofyear",
    "endofmonth", "endofquarter", "endofyear", "window", "offset",
}
_DAX_TOTAL = {"all", "allselected", "allexcept", "allnoblankrow", "removefilters", "allcrossfiltered"}
_DAX_SUM = {"sum", "sumx"}
_DAX_COUNT = {"count", "counta", "countrows", "countx", "countax", "countblank"}
# Wrappers that keep whatever their first argument is.
_DAX_KEEP_FIRST = {"calculate", "coalesce", "round", "roundup", "rounddown", "int", "abs", "keepfilters"}


def of_dax(expression: str, measures: Optional[Callable[[str], Optional[str]]] = None,
           _depth: int = 0) -> Additivity:
    """Classify a DAX measure expression.

    `measures(name)` returns the expression of another measure in the same
    model (or None), so `[Net Sales] - [Returns]` can be followed.
    """
    from app.data_sources.fast.lineage import dax_tokens

    tokens = dax_tokens(expression or "")
    if not tokens:
        return _unknown(UNREADABLE)
    if _depth > 6:
        return _unknown(COMPLEX)

    calls = {
        text.lower() for i, (kind, text) in enumerate(tokens)
        if kind == "word" and i + 1 < len(tokens) and tokens[i + 1][1] == "("
    }
    # A ratio or an average of anything is not additive, whatever it wraps.
    for names, reason in (
        (_DAX_DISTINCT, DISTINCT), (_DAX_AVERAGE, AVERAGE), (_DAX_RATIO, RATIO),
        (_DAX_TIME, TIME), (_DAX_STATISTIC, STATISTIC), (_DAX_MIN_MAX, MIN_MAX),
    ):
        if calls & names:
            return _no(reason)
    if any(kind == "punct" and text == "/" for kind, text in tokens):
        return _no(RATIO)
    if calls & _DAX_TOTAL:
        return _no(TOTAL)

    def term(ts: list) -> Optional[Additivity]:
        while len(ts) >= 2 and ts[0][1] == "(" and _closes(ts, 0) == len(ts) - 1:
            ts = ts[1:-1]
        if not ts:
            return None
        # constant * term
        parts = _split(ts, "*")
        if len(parts) > 1:
            rest = [p for p in parts if not (len(p) == 1 and p[0][0] == "number")]
            return term(rest[0]) if len(rest) == 1 else None
        if len(ts) == 1 and ts[0][0] == "bracket":
            if measures is None:
                return None
            inner = measures(ts[0][1][1:-1].replace("]]", "]").strip())
            return of_dax(inner, measures, _depth + 1) if inner else None
        if ts[0][0] == "word" and len(ts) >= 3 and ts[1][1] == "(" and _closes(ts, 1) == len(ts) - 1:
            name = ts[0][1].lower()
            if name in _DAX_SUM:
                return _yes(SUM)
            if name in _DAX_COUNT:
                # COUNTROWS(DISTINCT(...)) / COUNTROWS(VALUES(...)) count distinct values.
                inner_calls = {t.lower() for k, t in ts[2:] if k == "word"}
                return _no(DISTINCT) if inner_calls & _DAX_SET else _yes(COUNT)
            if name in _DAX_KEEP_FIRST:
                from app.data_sources.fast.lineage import _dax_args
                args = _dax_args(ts, 1)
                return term(args[0]) if args else None
        return None

    results = [term(part) for part in _split(tokens, "+", "-")]
    if any(r is None or r.additive == UNKNOWN for r in results):
        return _unknown(COMPLEX)
    for r in results:
        if r.additive is False:
            return r
    reasons = {r.reason for r in results}
    return _yes(reasons.pop() if len(reasons) == 1 else SUM)


def _closes(tokens: list, open_index: int) -> int:
    depth = 0
    for i in range(open_index, len(tokens)):
        text = tokens[i][1]
        if text in "({":
            depth += 1
        elif text in ")}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split(tokens: list, *operators: str) -> list:
    """Split at top-level operators (outside any parentheses)."""
    parts, current, depth = [], [], 0
    for kind, text in tokens:
        if text in "({":
            depth += 1
        elif text in ")}":
            depth -= 1
        if depth == 0 and kind == "punct" and text in operators and current:
            parts.append(current)
            current = []
            continue
        current.append((kind, text))
    if current:
        parts.append(current)
    return parts
