"""Structural check that an executed query is scoped by an identity value.

`verify_identity_binds_in_queries` (query_params) asks, for every executed
query, "does this query restrict its result rows to the viewer's identity
value?". The original answer was a text heuristic — the value appears after
the first WHERE/ON/HAVING — which `WHERE 'a@x' = 'a@x'`, `WHERE region='EU'
OR owner='a@x'` and `WHERE note <> 'a@x'` all satisfy. This module answers it
from the query's structure instead, per connector language:

  * **SQL** (any client sqlglot has a dialect for, unknown SQL-looking text via
    sqlglot's generic dialect, and Salesforce SOQL via the generic dialect):
    see `_sql_binds`. Rule, applied to the top-level statement:

      - A SELECT is *scoped* if a conjunct AND-connected to the root of its
        WHERE or HAVING (or of the ON of an INNER/plain JOIN, when the SELECT
        has no outer joins) is ``column = value``, ``value = column``,
        ``column IN (value[, other identity values])`` or ``column IN
        (scoped subquery)`` / ``column = (scoped subquery)``. LOWER/UPPER/
        TRIM/CAST may wrap either side. Under OR or NOT, literal-vs-literal,
        ``<>``, LIKE, and an IN list carrying any non-identity value do not
        count.
      - ...or if a row source every output row must come from is scoped: the
        FROM source (unless a RIGHT/FULL join exists) or an INNER/plain/CROSS
        joined source, where a source is a derived table or a CTE reference
        whose SELECT is itself scoped. (Every output row is built from one
        row of that source, so it inherits the restriction.)
      - Set operations: UNION — every branch; INTERSECT — any branch;
        EXCEPT — the left branch.
      - A statement that reads no table at all (``SELECT 1``) is scoped.

  * **Splunk SPL**: see `_spl_binds` — ``field=value`` in the base search
    (or a later ``| search``), implicitly AND-connected, not inside an OR
    group or under NOT. ``| where`` filters on the value are not modelled and
    fall back.

Anything this cannot decide (parse error, multiple statements, opaque
commands, SPL with subsearches / append / join, non-SQL API captures) returns
``None`` and the caller falls back to the legacy text heuristic, unchanged, so
existing widgets keep working. This is a guardrail against a forgotten or
mangled filter, not an isolation boundary: strict isolation belongs in the
database (RLS) or in per-user credentials (``auth_policy=user_required``).
"""

from __future__ import annotations

import functools
import json
import logging
import re
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

SPLUNK_CLIENT = "SplunkClient"
SOQL_CLIENTS = {"SalesforceClient"}


class CapturedQuery(str):
    """An executed query's text, tagged with the client class that ran it so
    the identity check can pick the right grammar. Behaves as a plain ``str``
    everywhere else (equality, JSON, logging)."""

    client_type: Optional[str]

    def __new__(cls, text: str, client_type: Optional[str] = None):
        obj = super().__new__(cls, text)
        obj.client_type = client_type
        return obj

    def __reduce__(self):
        return (CapturedQuery, (str(self), self.client_type))


def _matches(text: str, needles: Tuple[str, ...], fold: bool = False) -> bool:
    for n in needles:
        cands = {n, n.replace("''", "'")}
        if text in cands or (fold and text.casefold() in {c.casefold() for c in cands}):
            return True
    return False


# --------------------------------------------------------------------------- SQL

def _sql_binds(sql: str, dialect: Optional[str], needles: Tuple[str, ...]) -> Optional[bool]:
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import SqlglotError

    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except SqlglotError:
        return None
    except Exception:  # parser bug on odd input: never break the run on it
        return None
    if len(statements) != 1:
        return None
    root = statements[0]
    if not isinstance(root, (exp.Select, exp.SetOperation, exp.Subquery)):
        return None
    if not any(True for _ in root.find_all(exp.Table)):
        return True  # reads no table: no rows to leak

    ctes = {}
    for cte in root.find_all(exp.CTE):
        ctes.setdefault(cte.alias_or_name.lower(), []).append(cte.this)

    wrappers = (exp.Lower, exp.Upper, exp.Trim, exp.Cast, exp.TryCast, exp.Paren)

    def unwrap(node):
        fold = False
        while isinstance(node, wrappers):
            if isinstance(node, (exp.Lower, exp.Upper)):
                fold = True
            node = node.this
        return node, fold

    def is_column(node) -> bool:
        node, _ = unwrap(node)
        return isinstance(node, exp.Column) and not _matches(node.name, needles, fold=True)

    def is_value(node) -> bool:
        node, fold = unwrap(node)
        return isinstance(node, exp.Literal) and _matches(str(node.this), needles, fold=fold)

    def conjuncts(cond):
        while isinstance(cond, exp.Paren):
            cond = cond.this
        if isinstance(cond, exp.And):
            yield from conjuncts(cond.left)
            yield from conjuncts(cond.right)
        elif cond is not None:
            yield cond

    def pred_binds(cond, seen) -> bool:
        if isinstance(cond, (exp.Where, exp.Having)):
            cond = cond.this
        for c in conjuncts(cond):
            if isinstance(c, exp.EQ):
                l, r = c.left, c.right
                if (is_column(l) and is_value(r)) or (is_column(r) and is_value(l)):
                    return True
                for col, other in ((l, r), (r, l)):
                    if is_column(col) and isinstance(other, exp.Subquery) and query_scoped(other.this, seen):
                        return True
            elif isinstance(c, exp.In) and is_column(c.this):
                q = c.args.get("query")
                if q is not None:
                    if query_scoped(q.this if isinstance(q, exp.Subquery) else q, seen):
                        return True
                    continue
                items = c.expressions
                if items and all(is_value(i) for i in items):
                    return True
        return False

    def source_scoped(src, seen) -> bool:
        if isinstance(src, exp.Subquery):
            return query_scoped(src.this, seen)
        if isinstance(src, exp.Table) and not src.args.get("db") and src.name.lower() in ctes:
            key = src.name.lower()
            if key in seen:
                return False  # recursive CTE: don't chase it
            return all(query_scoped(q, seen | {key}) for q in ctes[key])
        return False

    def select_scoped(sel, seen) -> bool:
        if pred_binds(sel.args.get("where"), seen) or pred_binds(sel.args.get("having"), seen):
            return True
        joins = sel.args.get("joins") or []
        sides = {(j.side or "").upper() for j in joins}
        outer = bool(sides - {""})
        inner_kinds = {"", "INNER", "CROSS"}
        if not outer:
            for j in joins:
                if (j.kind or "").upper() in {"", "INNER"} and pred_binds(j.args.get("on"), seen):
                    return True
        frm = sel.args.get("from")
        if frm is not None and not (sides & {"RIGHT", "FULL"}) and source_scoped(frm.this, seen):
            return True
        for j in joins:
            if not (j.side or "") and (j.kind or "").upper() in inner_kinds and source_scoped(j.this, seen):
                return True
        if frm is None and not any(True for _ in sel.find_all(exp.Table)):
            return True
        return False

    def query_scoped(q, seen) -> bool:
        while isinstance(q, (exp.Subquery, exp.Paren)):
            q = q.this
        if isinstance(q, exp.Union):
            return query_scoped(q.left, seen) and query_scoped(q.right, seen)
        if isinstance(q, exp.Intersect):
            return query_scoped(q.left, seen) or query_scoped(q.right, seen)
        if isinstance(q, exp.Except):
            return query_scoped(q.left, seen)
        if isinstance(q, exp.Select):
            return select_scoped(q, seen)
        return False

    return query_scoped(root, frozenset())


# --------------------------------------------------------------------------- SPL

_SPL_WIDENING = re.compile(
    r"\|\s*(append|appendcols|appendpipe|join|union|multisearch|map|set|inputlookup|from)\b", re.I
)
_SPL_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|\(|\)|[^\s()"]+(?:"(?:[^"\\]|\\.)*")?')
_SPL_PAIR = re.compile(r'^([A-Za-z_][\w.:-]*)=("(?:[^"\\]|\\.)*"|[^"=<>!]+)$')


def _spl_text(capture: str) -> Optional[str]:
    s = capture.strip()
    if s.startswith("{"):
        try:
            env = json.loads(s)
        except ValueError:
            return None
        if not isinstance(env, dict) or not isinstance(env.get("spl"), str):
            return None  # dashboard / saved_search envelopes: SPL not visible
        s = env["spl"].strip()
    return s


def _spl_binds(capture: str, needles: Tuple[str, ...]) -> Optional[bool]:
    spl = _spl_text(capture)
    if not spl or spl.startswith("|") or "[" in spl or "`" in spl:
        return None  # generating command, subsearch, or macro: can't see the base
    if _SPL_WIDENING.search(spl):
        return None
    # split into pipeline segments on pipes outside quotes
    segments, start, in_q, i = [], 0, False, 0
    while i < len(spl):
        ch = spl[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            in_q = not in_q
        elif ch == "|" and not in_q:
            segments.append(spl[start:i])
            start = i + 1
        i += 1
    if in_q:
        return None
    segments.append(spl[start:])

    # The base search, and any later `| search` (same grammar, can only
    # narrow), may carry the bind. Other commands can't add rows (the ones that
    # can were refused above), so they neither help nor hurt.
    searches = [segments[0]] + [
        seg for seg in segments[1:] if seg.strip().lower().startswith("search ")
    ]
    verdicts = [_spl_search_binds(seg, needles) for seg in searches]
    if any(v is True for v in verdicts):
        return True
    if any(v is None for v in verdicts):
        return None
    if any(
        seg.strip().lower().startswith("where ") and any(n in seg for n in needles)
        for seg in segments[1:]
    ):
        return None  # `| where` expression filter: not modelled -> heuristic
    return False


def _spl_search_binds(text: str, needles: Tuple[str, ...]) -> Optional[bool]:
    tokens = _SPL_TOKEN.findall(text)
    if tokens and tokens[0].lower() == "search":
        tokens = tokens[1:]
    if any(t == "=" for t in tokens):
        return None  # `field = value` with spaces: shape we don't model

    def group(pos):
        """Parse tokens from pos until ')' -> (items, pos). An item is
        ('pair', field, value) / ('group', items) / ('op', text) /
        ('other', t) / ('term', t)."""
        items = []
        while pos < len(tokens):
            t = tokens[pos]
            if t == "(":
                sub, pos = group(pos + 1)
                items.append(("group", sub))
                continue
            if t == ")":
                return items, pos + 1
            if t in ("OR", "NOT", "AND"):
                items.append(("op", t))
            else:
                m = _SPL_PAIR.match(t)
                if m:
                    v = m.group(2)
                    if v.startswith('"'):
                        v = v[1:-1].replace('\\"', '"')
                    items.append(("pair", m.group(1), v))
                elif "=" in t:
                    items.append(("other", t))
                else:
                    items.append(("term", t))
            pos += 1
        return items, pos

    items, _ = group(0)

    def scoped(items) -> bool:
        if any(it[0] == "op" and it[1] == "OR" for it in items):
            return False  # an OR at this level widens every term in it
        for idx, it in enumerate(items):
            if idx > 0 and items[idx - 1] == ("op", "NOT"):
                continue
            if it[0] == "pair" and _matches(it[2], needles):
                return True
            if it[0] == "group" and scoped(it[1]):
                return True
        return False

    return scoped(items)


# ------------------------------------------------------------------------ entry

def _dialect_for(client_type: Optional[str]) -> Optional[str]:
    from app.ai.code_execution.sql_guard import CLIENT_DIALECTS

    return CLIENT_DIALECTS.get(client_type or "")


@functools.lru_cache(maxsize=2048)
def structural_binds(text: str, client_type: Optional[str], needles: Tuple[str, ...], looks_sql: bool) -> Optional[bool]:
    """True/False when the query's structure decides it; None when it can't
    (caller falls back to the text heuristic). Cached per query string."""
    if client_type == SPLUNK_CLIENT:
        return _spl_binds(text, needles)
    if not looks_sql:
        return None
    return _sql_binds(text, _dialect_for(client_type), needles)


def log_fallback(capture: str, client_type: Optional[str], param: str) -> None:
    logger.warning(
        "identity_bind_check.fallback",
        extra={
            "event": "identity_bind_check_fallback",
            "client_type": client_type or "unknown",
            "param": param,
            "query_preview": capture[:200],
        },
    )
