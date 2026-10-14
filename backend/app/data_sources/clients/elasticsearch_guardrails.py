"""Admin-set query guardrails for the Elasticsearch connector.

Every agent query passes through here before it reaches the cluster. Two kinds
of rule live side by side, and they fail in opposite directions on purpose:

* **Target rules** (all-index search, allowed patterns) are access rules. A
  target BOW cannot read is a target it cannot vouch for, so those fail closed.
* **Aggregation limits** are cost rules on a free-form JSON tree. A shape the
  analyzer does not understand is not evidence of an expensive query, so the
  analyzer fails *open*: any error while measuring logs a warning and lets the
  query through. The worst a checker bug may cause is "the limit did not apply
  this time" — never "a legitimate query was refused". The cluster's own
  `search.max_buckets` remains the backstop.
"""
from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Keys inside an aggregation body that are not the aggregation type itself.
_AGG_META_KEYS = {"aggs", "aggregations", "meta"}

# Bucket aggregations whose bucket count is set by `size`, with the engine's
# default when `size` is omitted. Others (histograms, ranges, filters…) have no
# size knob; they count toward the aggregation total but not the bucket
# estimate.
_SIZED_BUCKET_AGGS = {
    "terms": 10,
    "multi_terms": 10,
    "significant_terms": 10,
    "significant_text": 10,
    "composite": 10,
    "categorize_text": 10,
}

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(nanos|micros|ms|s|m|h|d)?\s*$", re.I)
_DURATION_UNITS = {
    "nanos": 1e-9, "micros": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600, "d": 86400,
}


class QueryGuardrailError(ValueError):
    """A query was refused by an admin-set limit on this connection.

    A ValueError so it surfaces to the agent like any other query error; the
    message says which limit and how to rewrite the query.
    """


def positive_int(value: Any) -> int | None:
    """Coerce a config value to a positive int, or None for "not set".

    Form round-trips deliver "", None, "20" or 20.0; anything that is not a
    positive number means "no limit" rather than an error.
    """
    if isinstance(value, bool):
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def parse_duration_seconds(value: Any) -> float | None:
    """Parse an Elasticsearch time value ("30s", "1m", "500ms") to seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 1000.0  # bare numbers are milliseconds
    if not isinstance(value, str):
        return None
    m = _DURATION_RE.match(value)
    if not m:
        return None
    return float(m.group(1)) * _DURATION_UNITS[(m.group(2) or "ms").lower()]


def split_patterns(text: str | None) -> list[str]:
    if not isinstance(text, str):
        return []
    return [p.strip() for p in text.split(",") if p.strip()]


# ---------------------------------------------------------------- targets --- #

def _local_name(part: str) -> str:
    """Strip a cross-cluster prefix (`remote:logs-*` -> `logs-*`)."""
    return part.rsplit(":", 1)[-1]


def is_all_indices_part(part: str) -> bool:
    """True for a target part that names every index: `_all`, `*`, `**`, `.*`."""
    name = _local_name(part.strip())
    if name in ("", "_all"):
        return True
    return name.strip("*?.") == ""


# Characters Elasticsearch forbids in index names. A target carrying one is not
# a real index name but something the URL path would reinterpret: `%2A` is
# decoded to `*` by the server and `/` re-routes the request, either of which
# would slip past the glob checks below.
_INVALID_TARGET_CHARS = set('/\\?#%" <>|')


def check_target(target: str | None, *, block_all: bool,
                 allowed_patterns: list[str]) -> None:
    """Refuse a search target that is all-index or outside the allowlist.

    Exclusions (`-logs-old`) only narrow a target, so they are ignored when
    deciding whether the target is too broad.
    """
    parts = [p.strip() for p in (target or "").split(",") if p.strip()]
    bad = [p for p in parts if _INVALID_TARGET_CHARS & set(p)]
    if bad:
        raise QueryGuardrailError(
            f"Invalid index target {', '.join(repr(p) for p in bad)}: index names "
            f"cannot contain any of / \\ ? # % \" < > | or spaces."
        )
    inclusions = [p for p in parts if not p.startswith("-")]
    if block_all and (not inclusions or any(is_all_indices_part(p) for p in inclusions)):
        hint = (f"one of: {', '.join(allowed_patterns)}" if allowed_patterns
                else "the specific index or pattern from the schema")
        raise QueryGuardrailError(
            f"Searching all indices is blocked on this connection (target "
            f"'{target or ''}'). Target {hint} instead."
        )
    if allowed_patterns:
        outside = [p for p in inclusions
                   if not any(fnmatch.fnmatchcase(_local_name(p), a) for a in allowed_patterns)]
        if outside:
            raise QueryGuardrailError(
                f"Index target {', '.join(repr(p) for p in outside)} is outside "
                f"what this connection may query. Allowed: "
                f"{', '.join(allowed_patterns)}."
            )


# ------------------------------------------------- SQL / ES|QL targets --- #
#
# Index names live in query *text* here, so finding them means lexing the text
# the way the engine does. Regex stripping is not enough: `'--'` inside a
# string literal would read as a comment and swallow the FROM that follows it.
# Each lexer walks the text once, tracking quote and comment state, and an
# input it cannot lex (an unterminated quote or comment) is refused — these
# are access checks, so they fail closed.

@dataclass
class _Tok:
    kind: str    # word | quoted | string | punct
    value: str


def _lex(text: str, *, line_comment: str, string_quote: str,
         ident_quotes: str, escape: str) -> list[_Tok]:
    """Tokenize SQL or ES|QL text.

    string_quote: the literal quote (`'` for SQL, `"` for ES|QL).
    ident_quotes: identifier quotes, whose content is a name (an index).
    escape: "double" (`''` inside '…') or "backslash" (`\"` inside "…").
    Comments and string literals are dropped; quoted identifiers are kept.
    """
    toks: list[_Tok] = []
    i, n = 0, len(text or "")
    punct = "(),|;"
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif text.startswith(line_comment, i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise QueryGuardrailError("Unterminated /* comment in the query.")
            i = j + 2
        elif string_quote == '"' and text.startswith('"""', i):
            j = text.find('"""', i + 3)
            if j < 0:
                raise QueryGuardrailError('Unterminated """ string in the query.')
            toks.append(_Tok("string", text[i + 3:j]))
            i = j + 3
        elif c == string_quote or c in ident_quotes:
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise QueryGuardrailError(f"Unterminated {c} quote in the query.")
                ch = text[j]
                if escape == "backslash" and ch == "\\" and c == string_quote:
                    buf.append(text[j:j + 2])
                    j += 2
                    continue
                if ch == c:
                    if j + 1 < n and text[j + 1] == c:   # doubled quote = escaped
                        buf.append(c)
                        j += 2
                        continue
                    break
                buf.append(ch)
                j += 1
            toks.append(_Tok("string" if c == string_quote else "quoted", "".join(buf)))
            i = j + 1
        elif c in punct:
            toks.append(_Tok("punct", c))
            i += 1
        else:
            j = i
            while (j < n and not text[j].isspace() and text[j] not in punct
                   and text[j] != string_quote and text[j] not in ident_quotes
                   and not text.startswith(line_comment, j) and not text.startswith("/*", j)):
                j += 1
            toks.append(_Tok("word", text[i:j]))
            i = j
    return toks


def _is_name(t: _Tok | None) -> bool:
    return t is not None and t.kind in ("word", "quoted")


def sql_targets(sql: str) -> list[str]:
    """Index names after each FROM that introduces a relation in an
    Elasticsearch SQL statement (lexical rules: '…' literals with '' escapes,
    "…" and `…` identifiers, -- and /* */ comments).

    FROM inside a function call — `EXTRACT(YEAR FROM ts)` — is not a relation;
    FROM inside a subquery — `(SELECT … FROM x)` — is.
    """
    toks = _lex(sql, line_comment="--", string_quote="'", ident_quotes='"`',
                escape="double")
    targets: list[str] = []
    parens: list[bool] = []          # per open paren: is it a subquery?
    for k, t in enumerate(toks):
        nxt = toks[k + 1] if k + 1 < len(toks) else None
        if t.kind == "punct" and t.value == "(":
            parens.append(nxt is not None and nxt.kind == "word" and nxt.value.upper() == "SELECT")
        elif t.kind == "punct" and t.value == ")":
            if parens:
                parens.pop()
        elif t.kind == "word" and t.value.upper() in ("FROM", "IN") and (not parens or parens[-1]):
            if t.value.upper() == "IN":
                # Only `SHOW COLUMNS IN <index>` names an index; `x IN (…)` doesn't.
                prev = toks[k - 1] if k else None
                if not (prev and prev.kind == "word" and prev.value.upper() == "COLUMNS"):
                    continue
            if _is_name(nxt):
                targets.append(nxt.value)
    return targets


# ES|QL commands that read no index. A command outside this set and the
# index-bearing ones below is refused while target rules are active: a new
# command could read an index the checks do not know to look at.
_ESQL_PLAIN_COMMANDS = {
    "WHERE", "EVAL", "STATS", "INLINESTATS", "INLINE", "KEEP", "DROP", "RENAME",
    "SORT", "LIMIT", "DISSECT", "GROK", "MV_EXPAND", "FORK", "CHANGE_POINT",
    "SAMPLE", "COMPLETION", "RERANK", "FUSE", "ROW", "SHOW", "EXPLAIN",
}


@dataclass
class EsqlSources:
    indices: list[str]               # FROM / TS / LOOKUP JOIN targets
    enrich_policies: list[str]
    unknown_commands: list[str]


def esql_sources(esql: str) -> EsqlSources:
    """Every index an ES|QL query reads, command by command: the FROM/TS
    source (and `(FROM …)` subqueries), each LOOKUP JOIN index, plus ENRICH
    policies and any command this parser does not recognise."""
    toks = _lex(esql, line_comment="//", string_quote='"', ident_quotes="`",
                escape="backslash")
    out = EsqlSources([], [], [])
    k = 0
    while k < len(toks):
        t = toks[k]
        prev = toks[k - 1] if k else None
        at_command = prev is None or (prev.kind == "punct" and prev.value == "|")
        after_paren = prev is not None and prev.kind == "punct" and prev.value == "("
        word = t.value.upper() if t.kind == "word" else None
        nxt = toks[k + 1] if k + 1 < len(toks) else None
        is_subquery = (after_paren and word == "FROM"
                       and nxt is not None and (_is_name(nxt) or nxt.kind == "string"))
        if word in ("FROM", "TS") and (at_command or is_subquery):
            j = k + 1
            # Source list: names separated by commas, up to METADATA / | / ( / ).
            while j < len(toks) and (_is_name(toks[j]) or toks[j].kind == "string"
                                     or (toks[j].kind == "punct" and toks[j].value == ",")):
                tj = toks[j]
                if tj.kind == "word" and tj.value.upper() == "METADATA":
                    break
                if tj.kind != "punct":
                    out.indices.append(tj.value)
                j += 1
            if j == k + 1 and not (j < len(toks) and toks[j].kind == "punct" and toks[j].value == "("):
                out.indices.append("")      # a source command with no source
            k = j
            continue
        if at_command and word == "LOOKUP":
            j = k + 1
            if j < len(toks) and toks[j].kind == "word" and toks[j].value.upper() == "JOIN":
                j += 1
            named = j < len(toks) and (_is_name(toks[j]) or toks[j].kind == "string")
            out.indices.append(toks[j].value if named else "")
            k = j + 1
            continue
        if at_command and word == "ENRICH":
            out.enrich_policies.append(nxt.value if _is_name(nxt) or (nxt and nxt.kind == "string") else "")
        elif at_command and t.kind == "word" and word not in _ESQL_PLAIN_COMMANDS:
            out.unknown_commands.append(t.value)
        k += 1
    return out


# ----------------------------------------------------------- aggregations --- #

@dataclass
class AggStats:
    count: int = 0          # aggregations in the tree, all levels
    depth: int = 0          # deepest nesting level (top level = 1)
    max_size: int = 0       # largest single sized-bucket `size`
    buckets: int = 0        # estimated total buckets the request asks for


def _agg_type(body: dict[str, Any]) -> str | None:
    kinds = [k for k in body if k not in _AGG_META_KEYS]
    return kinds[0] if kinds else None


def _sized_bucket_size(kind: str | None, spec: Any) -> int | None:
    if kind not in _SIZED_BUCKET_AGGS:
        return None
    raw = spec.get("size") if isinstance(spec, dict) else None
    if raw is None:
        return _SIZED_BUCKET_AGGS[kind]
    return int(raw)  # a non-numeric size raises -> caller fails open


def analyze_aggs(aggs: dict[str, Any], level: int = 1) -> AggStats:
    """Measure an aggregation tree. Raises on shapes it cannot read."""
    stats = AggStats()
    if not isinstance(aggs, dict):
        raise TypeError("aggs must be an object")
    for _name, body in aggs.items():
        if not isinstance(body, dict):
            raise TypeError("aggregation body must be an object")
        kind = _agg_type(body)
        stats.count += 1
        stats.depth = max(stats.depth, level)
        children = body.get("aggs") or body.get("aggregations") or {}
        sub = analyze_aggs(children, level + 1) if children else AggStats()
        stats.count += sub.count
        stats.depth = max(stats.depth, sub.depth)
        stats.max_size = max(stats.max_size, sub.max_size)
        size = _sized_bucket_size(kind, body.get(kind))
        if size is not None:
            stats.max_size = max(stats.max_size, size)
            # Each of this level's buckets carries its own copy of the
            # sub-tree's buckets: terms(50) > terms(10) is 50 + 50*10.
            stats.buckets += size * (1 + sub.buckets)
        else:
            stats.buckets += sub.buckets
    return stats


@dataclass
class AggLimits:
    max_aggregations: int | None = None
    max_depth: int | None = None
    max_buckets: int | None = None
    observe_only: bool = False

    @property
    def active(self) -> bool:
        return any((self.max_aggregations, self.max_depth, self.max_buckets))


def check_aggregations(aggs: Any, limits: AggLimits,
                       connection: str | None = None) -> None:
    """Enforce the aggregation limits — fail-open on anything unexpected."""
    if not limits.active or not aggs:
        return
    try:
        stats = analyze_aggs(aggs)
    except Exception as e:
        logger.warning(
            "Elasticsearch aggregation limits skipped for %s: could not analyze "
            "the aggregation tree (%s: %s); the query runs unchecked.",
            connection or "connection", type(e).__name__, e,
        )
        return

    violations: list[str] = []
    if limits.max_aggregations and stats.count > limits.max_aggregations:
        violations.append(
            f"{stats.count} aggregations (limit {limits.max_aggregations})")
    if limits.max_depth and stats.depth > limits.max_depth:
        violations.append(
            f"{stats.depth} nested aggregation levels (limit {limits.max_depth})")
    if limits.max_buckets and max(stats.max_size, stats.buckets) > limits.max_buckets:
        violations.append(
            f"~{max(stats.max_size, stats.buckets):,} buckets requested "
            f"(limit {limits.max_buckets:,}; terms/composite sizes multiply "
            f"across nested levels)")
    if not violations:
        return
    detail = "; ".join(violations)
    if limits.observe_only:
        logger.warning(
            "Elasticsearch aggregation limits (log-only) on %s would block a "
            "query: %s", connection or "connection", detail)
        return
    raise QueryGuardrailError(
        f"Query exceeds this connection's aggregation limits: {detail}. "
        f"Reduce the number of aggregations, nesting, or terms/composite size "
        f"— or split it into several smaller queries."
    )
