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


def _strip_comments_and_literals(text: str, line_comment: str) -> str:
    """Blank out comments and '…' string literals so a keyword inside them is
    not mistaken for query structure (and a leading comment cannot hide one)."""
    text = re.sub(r"/\*.*?\*/", " ", text or "", flags=re.DOTALL)
    text = re.sub(re.escape(line_comment) + r"[^\n]*", " ", text)
    return re.sub(r"'(?:[^']|'')*'", "''", text)


_SQL_FROM_RE = re.compile(r"\bFROM\s+(?:\"([^\"]+)\"|`([^`]+)`|([^\s,;()]+))", re.IGNORECASE)


def sql_targets(sql: str) -> list[str]:
    """Index names after each FROM that introduces a relation.

    Skips FROM inside a function call — `EXTRACT(YEAR FROM ts)`,
    `TRIM(LEADING 'x' FROM s)` — by checking the innermost open parenthesis:
    only a subquery (`(SELECT …`) owns a real FROM.
    """
    text = _strip_comments_and_literals(sql, "--")
    targets = []
    for m in _SQL_FROM_RE.finditer(text):
        depth_opens = []
        for i, ch in enumerate(text[:m.start()]):
            if ch == "(":
                depth_opens.append(i)
            elif ch == ")" and depth_opens:
                depth_opens.pop()
        if depth_opens and not re.match(r"\(\s*SELECT\b", text[depth_opens[-1]:], re.IGNORECASE):
            continue
        targets.append(next(g for g in m.groups() if g))
    return targets


_ESQL_SOURCE_RE = re.compile(r"^\s*(?:FROM|TS)\s+(.+?)(?:\s+METADATA\b|\||$)",
                             re.IGNORECASE | re.DOTALL)


def esql_target(esql: str) -> str | None:
    """The source list of an ES|QL `FROM a,b | …` (or `TS …`) query; None for
    source commands that read no index (ROW, SHOW)."""
    m = _ESQL_SOURCE_RE.match(_strip_comments_and_literals(esql, "//"))
    if not m:
        return None
    return ",".join(p.strip().strip('"') for p in m.group(1).split(","))


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
