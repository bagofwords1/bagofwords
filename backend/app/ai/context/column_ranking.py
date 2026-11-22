"""Rank the columns of a wide table and cap what reaches the agent's context.

Tables already get usage tracking, decayed ranking, a top-k cap and an index
of the rest. This module gives columns inside a table the same treatment:

* :func:`rank_table_columns` scores every column of a table from
  - usage (``ColumnStats``) with the same shape as the table score in
    ``schema_context_builder``: role-weighted usage decayed by age, success
    rate, net feedback;
  - instruction references (``InstructionReference.column_name``);
  - structural signals: primary / foreign key, timestamp, id-like name;
  and orders the columns best first (ties keep catalog order). Cold start
  (no usage yet) is structural signals plus catalog order.
* :func:`curate_columns` keeps the top ``budget`` columns and folds the rest
  into prefix groups (``svc.request.* (412)``, ``cpu_* (230)``) or a plain
  "+M more", with an honest account of why each shown column was chosen.

Context curation only: the folded columns stay queryable and searchable
(describe_tables ``columns``); nothing here is an access boundary.
"""
from __future__ import annotations

import math
import re
from functools import lru_cache
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

# Default columns rendered in full per table. Org setting: top_k_schema_columns.
DEFAULT_COLUMN_BUDGET = 60
# Fold summary: at most this many prefix groups are named.
MAX_FOLD_GROUPS = 12

_MULTI_FIELD_SUFFIXES = ("keyword", "raw", "text", "lowercase", "normalized")
_TIME_DTYPE = re.compile(r"\b(date|time|timestamp|datetime)", re.I)
_TIME_NAME = re.compile(r"(^|[._])(@?timestamp|ts|time|date|created|updated|modified|occurred|event_time)([._]|$)|(_at|_date|_time|_ts)$", re.I)
_ID_NAME = re.compile(r"(^|[._])(id|uuid|guid|key)$|(_id|Id|ID|_key|_uuid)$")


# Structural signals at or above this lift a column ahead of the sampled rest
# (keys, named timestamps, id-like names). A merely date-typed field stays
# below it: a search index can carry hundreds of them, and letting them all
# outrank everything else filled the budget with arbitrary date attributes.
STRONG_STRUCTURE = 0.5


@lru_cache(maxsize=262_144)
def _name_signal(name: str, dtype: str) -> float:
    """Timestamp / id-like signal from a column's name and type. Memoized:
    wide catalogs repeat the same (name, dtype) on every schema build."""
    if _TIME_NAME.search(name):
        return 0.6
    if _ID_NAME.search(name):
        return 0.5
    if _TIME_DTYPE.search(dtype):
        return 0.1
    return 0.0


def structural_signal(name: str, dtype: Optional[str], pk_names: Set[str], fk_names: Set[str]) -> float:
    n = (name or "").lower()
    if n in pk_names:
        return 1.0
    if n in fk_names:
        return 0.8
    return _name_signal(name or "", dtype or "")


def usage_signal(stats, now: Optional[datetime] = None) -> float:
    """Same shape as the table score: 0.35 * sqrt(weighted usage) * recency
    + 0.25 * success rate + 0.2 * net weighted feedback. Failures never
    subtract here; they add less weighted usage at write time."""
    if stats is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    usage_count = int(getattr(stats, "usage_count", 0) or 0)
    success_count = int(getattr(stats, "success_count", 0) or 0)
    weighted = float(getattr(stats, "weighted_usage_count", 0.0) or 0.0)
    last = getattr(stats, "last_used_at", None)
    if last is not None:
        age_days = max(0.0, (now - last.replace(tzinfo=timezone.utc)).total_seconds() / 86400.0)
    else:
        age_days = 365.0
    recency = math.exp(-age_days / 14.0)
    success_rate = (success_count / usage_count) if usage_count > 0 else 0.0
    feedback = float(getattr(stats, "weighted_pos_feedback", 0.0) or 0.0) - float(getattr(stats, "weighted_neg_feedback", 0.0) or 0.0)
    return 0.35 * math.sqrt(max(0.0, weighted)) * recency + 0.25 * success_rate + 0.2 * feedback


def _names(cols) -> Set[str]:
    out = set()
    for c in cols or []:
        name = c.get("name") if isinstance(c, dict) else getattr(c, "name", None)
        if name:
            out.add(str(name).lower())
    return out


def rank_table_columns(table, stats_by_col: Optional[Dict[str, object]] = None,
                       instruction_cols: Optional[Set[str]] = None,
                       now: Optional[datetime] = None) -> None:
    """Score and order ``table.columns`` in place (best first). Sets per-column
    ``score``/``usage_count``/... and ``table.columns_ranked``."""
    cols = list(getattr(table, "columns", None) or [])
    if not cols:
        table.columns_ranked = True
        return
    stats_by_col = stats_by_col or {}
    instruction_cols = {c.lower() for c in (instruction_cols or set())}
    pk_names = _names(getattr(table, "pks", None))
    fk_names = {str(getattr(getattr(fk, "column", None), "name", "") or "").lower()
                for fk in (getattr(table, "fks", None) or [])}
    now = now or datetime.now(timezone.utc)
    lows = [str(c.name).lower() for c in cols]
    n = len(cols)
    usage = [0.0] * n
    instr = [0.0] * n
    struct = [0.0] * n
    index_of = {}
    stats_at: Dict[int, object] = {}
    for i, (c, low) in enumerate(zip(cols, lows)):
        index_of.setdefault(low, i)
        struct[i] = structural_signal(c.name, getattr(c, "dtype", None), pk_names, fk_names)
        if low in instruction_cols:
            instr[i] = 0.5
        s = stats_by_col.get(low) if stats_by_col else None
        if s is not None:
            usage[i] = usage_signal(s, now)
            stats_at[i] = s
    # A multi-field (`x.keyword`) rides with its used parent `x`: usage was
    # recorded on the field, but the agent aggregates on the variant.
    if any(usage) or any(instr):
        for i, low in enumerate(lows):
            if usage[i] or instr[i]:
                continue
            base, _, sub = low.rpartition(".")
            if base and sub in _MULTI_FIELD_SUFFIXES:
                j = index_of.get(base)
                if j is not None and (usage[j] or instr[j]):
                    usage[i] = usage[j] * 0.95
                    instr[i] = instr[j] * 0.95
    scores = [usage[i] + instr[i] + 0.2 * struct[i] for i in range(n)]
    ranked = sorted(range(n), key=lambda i: (-scores[i], i))
    lead = [i for i in ranked if usage[i] or instr[i] or struct[i] >= STRONG_STRUCTURE]
    lead_set = set(lead)
    rest = [i for i in ranked if i not in lead_set]
    # The rest has no evidence for any column, so instead of the first N in
    # catalog order (all `geo.client.*` on a search index) it is a sample of
    # every column family, round-robin by prefix, best-scored first within
    # each family. The model then sees what kinds of columns exist.
    sampled = False
    families: Dict[str, List[int]] = {}
    for i in rest:
        families.setdefault(_group_key(cols[i].name) or "", []).append(i)
    if len(families) > 1:
        from itertools import zip_longest
        rest = [i for row in zip_longest(*families.values()) for i in row if i is not None]
        sampled = True
    budget = getattr(table, "column_budget", None)
    budget = DEFAULT_COLUMN_BUDGET if budget is None else budget
    final = lead + rest
    # Only the slots that can be shown get per-column attributes (usage
    # counts, score, why): pydantic setattr is the dominant cost of ranking,
    # and a heavily used wide table brings hundreds of stats rows.
    visible = final if budget <= 0 else final[:budget]
    for i in visible:
        c = cols[i]
        s = stats_at.get(i)
        if s is not None:
            c.usage_count = int(getattr(s, "usage_count", 0) or 0)
            c.success_count = int(getattr(s, "success_count", 0) or 0)
            c.failure_count = int(getattr(s, "failure_count", 0) or 0)
            c.pos_feedback_count = int(getattr(s, "pos_feedback_count", 0) or 0)
            c.neg_feedback_count = int(getattr(s, "neg_feedback_count", 0) or 0)
        if instr[i]:
            c.referenced_by_instructions = True
        if scores[i]:
            c.score = round(scores[i], 6)
        if i in lead_set:
            c.selection_signal = "usage" if usage[i] > 0 else "instructions" if instr[i] > 0 else "structural"
        elif sampled:
            c.selection_signal = "sample"
    table.columns = [cols[i] for i in final]
    table.columns_ranked = True


@dataclass
class CuratedColumns:
    shown: List[object]
    folded: List[object]
    total: int
    budget: int
    reason: str                     # all_columns | usage_ranked | structural_only
    signal_counts: Dict[str, int] = field(default_factory=dict)
    groups: List[Tuple[str, int]] = field(default_factory=list)
    other: int = 0

    @property
    def capped(self) -> bool:
        return bool(self.folded)


def _group_key(name: str) -> Optional[str]:
    n = (name or "").replace("[]", "")
    if "." in n:
        return n.split(".", 1)[0]
    if "_" in n.strip("_"):
        return n.strip("_").split("_", 1)[0]
    return None


def fold_groups(names: Iterable[str], max_groups: int = MAX_FOLD_GROUPS) -> Tuple[List[Tuple[str, int]], int]:
    """Group folded column names by prefix: dotted paths by their first
    segment (split one level deeper when a group dominates), snake_case names
    by their first token. Returns ([(label, count)], ungrouped count)."""
    names = list(names)
    total = len(names)
    first = Counter()
    members: Dict[str, List[str]] = {}
    ungrouped = 0
    for n in names:
        k = _group_key(n)
        if k is None:
            ungrouped += 1
            continue
        first[k] += 1
        members.setdefault(k, []).append(n)
    groups: List[Tuple[str, int]] = []
    for k, cnt in first.items():
        sep = "." if any("." in m for m in members[k]) else "_"
        if cnt < 3:
            ungrouped += cnt
            continue
        if sep == "." and cnt > max(50, total // 4):
            second = Counter(
                ".".join(m.replace("[]", "").split(".")[:2]) for m in members[k] if m.replace("[]", "").count(".") >= 2
            )
            # Split a dominating group one level deeper (`svc.*` ->
            # `svc.request.*`, `svc.response.*`); a single child just gets the
            # more specific label.
            if second and sum(second.values()) == cnt:
                for k2, c2 in second.items():
                    groups.append((f"{k2}.*", c2))
                continue
        groups.append((f"{k}.*" if sep == "." else f"{k}_*", cnt))
    groups.sort(key=lambda g: (-g[1], g[0]))
    other = ungrouped + sum(c for _, c in groups[max_groups:])
    return groups[:max_groups], other


def curate_columns(table, budget: Optional[int] = None) -> CuratedColumns:
    """Top ``budget`` columns of a table (ranking structurally first when the
    builder did not rank it) and the folded remainder."""
    if budget is None:
        budget = getattr(table, "column_budget", None)
    if budget is None:
        budget = DEFAULT_COLUMN_BUDGET
    raw = getattr(table, "columns", None) or []
    memo = getattr(table, "_curated", None)
    # Holds the list itself (not its id): a replaced column list never hits.
    if memo is not None and memo[0] == budget and memo[1] is raw and memo[2] == len(raw):
        return memo[3]
    cur = _curate(table, list(raw), budget)
    try:
        final = getattr(table, "columns", None) or []  # ranking may replace it
        table._curated = (budget, final, len(final), cur)
    except Exception:
        pass
    return cur


def _curate(table, cols: list, budget: int) -> CuratedColumns:
    total = len(cols)
    if budget <= 0 or total <= budget:
        return CuratedColumns(shown=cols, folded=[], total=total, budget=budget, reason="all_columns")
    if not getattr(table, "columns_ranked", False):
        rank_table_columns(table)
        cols = list(table.columns or [])
    shown, folded = cols[:budget], cols[budget:]
    counts = Counter(getattr(c, "selection_signal", None) or "schema_order" for c in shown)
    reason = "usage_ranked" if (counts.get("usage") or counts.get("instructions") or counts.get("mentioned")) else "structural_only"
    groups, other = fold_groups([c.name for c in folded])
    return CuratedColumns(shown=shown, folded=folded, total=total, budget=budget, reason=reason,
                          signal_counts=dict(counts), groups=groups, other=other)


def fold_summary_text(cur: CuratedColumns) -> str:
    parts = [f"{label} ({cnt})" for label, cnt in cur.groups]
    if cur.other:
        parts.append(f"{cur.other} other column{'s' if cur.other != 1 else ''}" if parts else f"+{cur.other} more columns")
    return ", ".join(parts) if parts else f"+{len(cur.folded)} more columns"


def selection_note(cur: CuratedColumns) -> str:
    """Human-readable 'why these columns' (context panel, snapshot)."""
    if not cur.capped:
        return "all columns fit the budget"
    labels = {"mentioned": "named in the task", "usage": "by usage", "instructions": "referenced by instructions",
              "structural": "key/time/id columns", "sample": "sampled across column groups",
              "schema_order": "catalog order"}
    bits = [f"{cur.signal_counts[k]} {labels[k]}" for k in ("mentioned", "usage", "instructions", "structural", "sample", "schema_order")
            if cur.signal_counts.get(k)]
    return ", ".join(bits)


# ------------------------------------------------------------- column search
# describe_tables `columns`: at most this many matching columns are listed per
# table (the rest fold into the same prefix summary).
MAX_MATCHED_COLUMNS = 200
_REGEX_CHARS = re.compile(r"[\^\$\[\]\(\)\{\}\|\+\\]")


_SEGMENT_START = r"(?:^|(?<=[._\-\]]))"
_SEGMENT_END = r"(?:$|(?=[._\-\[]))"


def _wildcard_regex(term: str) -> str:
    """A ``*``/``?`` pattern matched against name *segments*: ``retrans*``
    finds ``net_retransmit_rate`` (a segment starts with it), ``svc.request.*``
    finds everything under that prefix, ``*_pct`` every name ending a segment
    in ``_pct``."""
    body = "".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in term)
    start = "" if term.startswith("*") else _SEGMENT_START
    end = "" if term.endswith("*") else _SEGMENT_END
    return f"{start}{body}{end}"


def column_term_matchers(terms: Iterable[str]) -> List[Tuple[str, object]]:
    """One predicate per describe_tables column-search term. Each term is
    matched case-insensitively against the column name and description as:
    * a regex when it uses regex syntax (``^svc\\.``, ``(user|account)_id``);
    * a wildcard pattern when it has ``*`` / ``?`` (see _wildcard_regex);
    * otherwise a plain substring (``retry`` matches ``svc.request.retry_budget``).
    Model-written patterns run through the bounded regex engine."""
    from app.utils.safe_regex import compile_bounded

    out: List[Tuple[str, object]] = []
    for raw in terms or []:
        term = (raw or "").strip()
        if not term:
            continue
        pat = None
        if _REGEX_CHARS.search(term):
            pat = compile_bounded(term, re.IGNORECASE)
        if pat is None and ("*" in term or "?" in term):
            pat = compile_bounded(_wildcard_regex(term), re.IGNORECASE)
        if pat is None:
            sub = term.lower()
            out.append((term, lambda name, desc, sub=sub: sub in name.lower() or (bool(desc) and sub in desc.lower())))
            continue

        def _m(name, desc, pat=pat):
            try:
                return bool(pat.search(name) or (desc and pat.search(desc)))
            except Exception:
                return False
        out.append((term, _m))
    return out


def column_matcher(terms: Iterable[str]):
    """Predicate: does a column match any of ``terms`` (column_term_matchers)."""
    matchers = column_term_matchers(terms)

    def match(col) -> bool:
        name = str(getattr(col, "name", "") or "")
        desc = str(getattr(col, "description", "") or "")
        return any(m(name, desc) for _, m in matchers)

    return match


def search_columns(cols: List[object], terms: Iterable[str]) -> List[object]:
    """Columns matching any term, most specific first: a column hit by a
    narrow term (few matches) leads columns hit only by a broad one, so a
    broad companion pattern (``net_*``) cannot push the column the agent is
    after past the listing cap. Input order is kept within each tier."""
    matchers = column_term_matchers(terms)
    if not matchers:
        return []
    hits: List[Tuple[object, List[int]]] = []
    counts = [0] * len(matchers)
    for c in cols:
        name = str(getattr(c, "name", "") or "")
        desc = str(getattr(c, "description", "") or "")
        which = [k for k, (_, m) in enumerate(matchers) if m(name, desc)]
        if which:
            hits.append((c, which))
            for k in which:
                counts[k] += 1
    ordered = sorted(enumerate(hits), key=lambda x: (min(counts[k] for k in x[1][1]), x[0]))
    return [c for _, (c, _) in ordered]


_IDENT = re.compile(r"[@A-Za-z_][\w@.\[\]-]*")


def promote_mentioned_columns(ctx, *texts: Optional[str]) -> int:
    """Move columns named in ``texts`` (the planner's task for the code
    generator) to the front of each wide table, so a column the planner found
    with describe_tables ``columns`` reaches the coder even when its usage
    rank would fold it. Returns the number of columns promoted."""
    tokens: Set[str] = set()
    for text in texts:
        for tok in _IDENT.findall(text or ""):
            low = tok.lower().strip(".-")
            tokens.add(low)
            # `x.keyword` in the task means the field `x` too.
            base, _, sub = low.rpartition(".")
            if base and sub in _MULTI_FIELD_SUFFIXES:
                tokens.add(base)
    if not tokens:
        return 0
    promoted = 0
    for ds in getattr(ctx, "data_sources", None) or []:
        for t in getattr(ds, "tables", None) or []:
            cols = list(getattr(t, "columns", None) or [])
            budget = getattr(t, "column_budget", None)
            budget = DEFAULT_COLUMN_BUDGET if budget is None else budget
            if budget <= 0 or len(cols) <= budget:
                continue
            if not getattr(t, "columns_ranked", False):
                rank_table_columns(t)
                cols = list(t.columns or [])
            hits = [c for c in cols if str(c.name).lower() in tokens]
            if not hits:
                continue
            for c in hits:
                c.selection_signal = "mentioned"
            hit_ids = {id(c) for c in hits}
            t.columns = hits + [c for c in cols if id(c) not in hit_ids]
            promoted += len(hits)
    return promoted
