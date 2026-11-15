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
        Table scope's `reads_rows` decides, so a table read without FROM
        (a ClickHouse IN set, a dictionary or Join table read by name)
        counts as a read.

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

from sqlglot.dialects.dialect import Dialect
from sqlglot.tokens import Tokenizer as _Tokenizer

logger = logging.getLogger(__name__)

SPLUNK_CLIENT = "SplunkClient"
# Captured as {"query", "params"}: judged by its own grammar (cypher_guard),
# never by the text heuristic, which would find the value in params.
NEO4J_CLIENT = "Neo4jClient"
SOQL_CLIENTS = {"SalesforceClient"}


class CapturedQuery(str):
    """An executed query's text, tagged with the client class that ran it so
    the identity check can pick the right grammar. Behaves as a plain ``str``
    everywhere else (equality, JSON, logging). `outside_root` marks the blank
    capture of a request that left the connection's configured path.
    `identity_reads`, when not None, is what the identity check reads instead
    of the text: for an OData call, one capture per request it sent (the text
    stays the query the code wrote, which users and the audit see).
    `literal_style` is the client's `sql_literal_style`: a capture with no
    known dialect is read with one whose string literals follow it
    (`_STYLE_DIALECTS`). `sql_in_json_envelope` is the client's own flag: its
    query is a JSON envelope whose "sql" field is the statement
    (Elasticsearch, OpenSearch)."""

    client_type: Optional[str]
    outside_root: bool
    identity_reads: Optional[Tuple["CapturedQuery", ...]]
    literal_style: Optional[str]
    sql_in_json_envelope: bool

    def __new__(cls, text: str, client_type: Optional[str] = None, outside_root: bool = False,
                identity_reads: Optional[Tuple["CapturedQuery", ...]] = None,
                literal_style: Optional[str] = None, sql_in_json_envelope: bool = False):
        obj = super().__new__(cls, text)
        obj.client_type = client_type
        obj.outside_root = outside_root
        obj.identity_reads = identity_reads
        obj.literal_style = literal_style
        obj.sql_in_json_envelope = bool(sql_in_json_envelope)
        return obj

    def __reduce__(self):
        return (CapturedQuery, (str(self), self.client_type, self.outside_root, self.identity_reads,
                                self.literal_style, self.sql_in_json_envelope))


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

    from app.ai.code_execution.table_scope import reads_rows

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
    if not reads_rows(root, dialect):
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
        if frm is None and not reads_rows(sel, dialect):
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
                        v = _renderer(SPLUNK_CLIENT).decode(v[1:-1])
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

def _renderer(client_type: Optional[str]):
    from app.ai.code_execution.query_params import renderer_for_name

    return renderer_for_name(client_type or "")


class _BowDoubledLiterals(Dialect):
    """sqlglot's generic dialect, except that a backslash in a string also
    escapes. Nothing else of any one engine (its comments, its other string
    forms) comes with it."""

    class Tokenizer(_Tokenizer):
        STRING_ESCAPES = ["'", "\\"]


# Registered with sqlglot under the class name, lowercased.
_DOUBLED_LITERALS = "_bowdoubledliterals"

# For a client with neither: a dialect whose string literals follow the
# client's literal style. "doubled": '' and a backslash both escape (the
# generic dialect otherwise). "backslash": only a backslash does, and a
# string may also be in double quotes (Spark, whose other forms are syntax
# such an engine refuses). ("standard" reads in sqlglot's generic dialect,
# where '' is the only escape.) Comments are not pinned by a style: see
# `_may_hold_a_comment`.
_STYLE_DIALECTS = {"doubled": _DOUBLED_LITERALS, "backslash": "spark"}


def _dialect_for(client_type: Optional[str], literal_style: Optional[str] = None) -> Optional[str]:
    """The sqlglot dialect a capture is parsed with: the guard's, else the one
    its parameter renderer writes literals for, so a rendered literal is read
    back as the value, else one whose literals follow the client's declared
    style (`_STYLE_DIALECTS`)."""
    return _pinned_dialect(client_type) or _STYLE_DIALECTS.get(literal_style or "")


def _pinned_dialect(client_type: Optional[str]) -> Optional[str]:
    """The guard's dialect for the client, else its renderer's: a reading
    pinned to the engine, not to a declared literal style."""
    from app.ai.code_execution.query_params import _sqlglot_dialect_of
    from app.ai.code_execution.sql_guard import CLIENT_DIALECTS

    return CLIENT_DIALECTS.get(client_type or "") or _sqlglot_dialect_of(_renderer(client_type))


def _as_standard_literals(text: str, client_type: Optional[str]) -> Optional[str]:
    """SOQL with each string literal rewritten as a standard one ('' for a
    quote) holding the same value, so sqlglot's generic dialect reads the
    value SOQL does. None when a literal is left open."""
    from app.ai.code_execution.query_params import _ANSI

    renderer = _renderer(client_type)
    spans = renderer.string_spans(text)
    # Only the last literal can be open; if it is, it also takes in text
    # added after the end.
    if spans and renderer.string_spans(text + " ")[-1][1] > len(text):
        return None
    out, last = [], 0
    for start, end in spans:
        out.append(text[last:start])
        out.append(_ANSI.quote(renderer.decode(text[start + 1:end - 1])))
        last = end
    out.append(text[last:])
    return "".join(out)


@functools.lru_cache(maxsize=2048)
def structural_binds(text: str, client_type: Optional[str], needles: Tuple[str, ...], looks_sql: bool,
                     literal_style: Optional[str] = None) -> Optional[bool]:
    """True/False when the query's structure decides it; None when it can't
    (caller falls back to the text heuristic). Cached per query string."""
    if client_type == SPLUNK_CLIENT:
        return _spl_binds(text, needles)
    if client_type == NEO4J_CLIENT:
        from app.data_sources.clients.cypher_guard import cypher_binds

        return cypher_binds(text, needles)
    if not looks_sql:
        return None
    if client_type in SOQL_CLIENTS:
        text = _as_standard_literals(text, client_type)
        if text is None:
            return None
        # Rewritten as standard literals: read where only '' escapes.
        literal_style = None
    renderer = _renderer(client_type)
    two_readings = renderer is not None and renderer.name == "ansi_or_backslash"
    readings = (None, _DOUBLED_LITERALS) if two_readings else (_dialect_for(client_type, literal_style),)
    if not _engine_reading(client_type) and _may_hold_a_comment(text, readings, _percent_comments(client_type)):
        # The reading is not the engine's own: where the engine starts or
        # ends a comment is not known, so the filter is not proven.
        return False
    if two_readings:
        # The engine may read a backslash in a string as an escape or not
        # (a server setting): the filter counts only when it binds in both
        # readings, which differ in that alone. Where they disagree, or
        # either cannot read the text, it is not bound.
        standard = _sql_binds(text, None, needles)
        backslash = _sql_binds(text, _DOUBLED_LITERALS, needles)
        if standard is None or backslash is None:
            return False
        return standard if standard == backslash else False
    return _sql_binds(text, readings[0], needles)


def _engine_reading(client_type: Optional[str]) -> bool:
    """Whether the identity check reads the client's captures with the
    guard's own dialect for it."""
    from app.ai.code_execution.sql_guard import CLIENT_DIALECTS

    return CLIENT_DIALECTS.get(client_type or "") is not None


@functools.lru_cache(maxsize=None)
def _percent_comments(client_type: Optional[str]) -> bool:
    """Whether the client's engine may read `%` as a line comment."""
    from app.ai.code_execution.client_profiles import _CLIENT_TABLE
    from app.ai.code_execution.sql_guard import _NONPARSING_PERCENT_COMMENTS

    return any(entry.family == client_type and entry.profile is not None
               and entry.profile.engine in _NONPARSING_PERCENT_COMMENTS for entry in _CLIENT_TABLE.values())


# What may open or close a comment in some engine.
_COMMENT_MARKS = ("--", "/*", "*/", "//", "#")


def _may_hold_a_comment(text: str, readings: Tuple[Optional[str], ...], percent: bool) -> bool:
    """Whether `text`, with its strings and double-quoted names emptied in
    any of `readings` or in the generic reading with or without backslash
    escapes, holds a comment mark (or `%`, when `percent`). A reading that
    cannot read the text empties nothing."""
    from app.ai.code_execution.sql_guard import _LITERAL_TOKENS, _masked

    marks = _COMMENT_MARKS + (("%",) if percent else ())
    for dialect in dict.fromkeys(readings + (None, _DOUBLED_LITERALS)):
        try:
            tokens = Dialect.get_or_raise(dialect).tokenize(text)
        except Exception:
            code = text
        else:
            code = _masked(text, [(t.start, t.end + 1) for t in tokens
                                  if t.token_type.name in _LITERAL_TOKENS
                                  or (t.token_type.name == "IDENTIFIER" and text.startswith('"', t.start))])
        if any(mark in code for mark in marks):
            return True
    return False


def _reading_of(client_type: Optional[str], literal_style: Optional[str]) -> str:
    """Which reading the identity check took of a capture, for the log."""
    renderer = _renderer(client_type)
    if renderer is not None and renderer.name == "ansi_or_backslash":
        return "standard and backslash-escape"
    if client_type in SOQL_CLIENTS:
        # Rewritten as standard literals and read in the generic dialect.
        return "generic"
    return _dialect_for(client_type, literal_style) or "generic"


def log_fallback(capture: str, client_type: Optional[str], param: str,
                 literal_style: Optional[str] = None) -> None:
    """Log that the structural check could not decide a capture, by structure
    only: the client type, the param's name, the capture's length and the
    reading taken. Never the text: it holds the identity value."""
    logger.warning(
        "identity_bind_check.fallback",
        extra={
            "event": "identity_bind_check_fallback",
            "client_type": client_type or "unknown",
            "param": param,
            "query_length": len(capture),
            "reading": "spl" if client_type == SPLUNK_CLIENT else _reading_of(client_type, literal_style),
        },
    )
