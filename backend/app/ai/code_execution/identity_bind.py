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
existing widgets keep working. Where the reading is not the engine's own (no
guard dialect for the client), SQL text outside a small allow-list, or text a
reading cannot decide, is refused instead (`structural_verdict`). This is a guardrail against a forgotten or
mangled filter, not an isolation boundary: strict isolation belongs in the
database (RLS) or in per-user credentials (``auth_policy=user_required``).
"""

from __future__ import annotations

import functools
import json
import logging
import re
import string
import unicodedata
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
# `_outside_the_allow_list`.
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
def structural_verdict(text: str, client_type: Optional[str], needles: Tuple[str, ...], looks_sql: bool,
                       literal_style: Optional[str] = None) -> Tuple[Optional[bool], Optional[str]]:
    """(verdict, reason): the verdict is True/False when the query's
    structure decides it, None when it can't (caller falls back to the text
    heuristic). The reason is set when the filter is unproven because the
    text holds syntax this check cannot verify for the client: "char",
    "sequence", "backslash", "unparseable" or "percent". Cached per query
    string."""
    if client_type == SPLUNK_CLIENT:
        return _spl_binds(text, needles), None
    if client_type == NEO4J_CLIENT:
        from app.data_sources.clients.cypher_guard import cypher_binds

        return cypher_binds(text, needles), None
    engine_reading = _engine_reading(client_type)
    if not looks_sql and (engine_reading or not _renders_sql(client_type)):
        return None, None
    if not engine_reading:
        # The reading is not the engine's own: the filter counts only when
        # the text is plain enough that every reasonable reading agrees.
        reason = _outside_the_allow_list(text, _percent_comments(client_type), _loose_operators(client_type))
        if reason is not None:
            return False, reason
        if not looks_sql:
            # The client takes SQL, and this does not read as a query.
            return False, "unparseable"
    if client_type in SOQL_CLIENTS:
        text = _as_standard_literals(text, client_type)
        if text is None:
            return (None, None) if engine_reading else (False, "unparseable")
        # Rewritten as standard literals: read where only '' escapes.
        literal_style = None
    renderer = _renderer(client_type)
    two_readings = renderer is not None and renderer.name == "ansi_or_backslash"
    readings = (None, _DOUBLED_LITERALS) if two_readings else (_dialect_for(client_type, literal_style),)
    verdicts = [_sql_binds(text, reading, needles) for reading in readings]
    if None in verdicts:
        # A reading that cannot read the text proves nothing; where it is
        # not the engine's own, the filter is not proven.
        return (None, None) if engine_reading else (False, "unparseable")
    # Two readings (the engine may read a backslash in a string as an escape
    # or not, a server setting): the filter counts only when it binds in both.
    return (verdicts[0] if len(set(verdicts)) == 1 else False), None


# Renderers whose text is not SQL: the identity check reads it another way.
_NOT_SQL_RENDERERS = frozenset({"odata", "kql", "spl"})


def _renders_sql(client_type: Optional[str]) -> bool:
    """Whether the client's parameter renderer writes SQL literals: its
    captures are SQL."""
    renderer = _renderer(client_type)
    return renderer is not None and renderer.name not in _NOT_SQL_RENDERERS


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


# Outside a '...' string: what a plain query is written with (`:` for a
# cast or a named placeholder, `?` for a placeholder; neither is a literal,
# so neither can stand for the identity value). A `;` is allowed only as
# the last character that is not whitespace (`_outside_the_allow_list`).
_ALLOWED_OUTSIDE_STRINGS = frozenset(string.ascii_letters + string.digits + "_ \t\n.,()=<>!*+-/|%:?")
# Inside a "..." name.
_ALLOWED_IN_NAMES = frozenset(string.ascii_letters + string.digits + "_ .-")
# Sequences some engine reads as a comment mark, and `??`, a coalesce that
# binds looser than AND/OR where an engine has it.
_REFUSED_SEQUENCES = ("--", "/*", "*/", "//", "??")
# Operators that bind looser than AND/OR on a family's engine (a ternary,
# a lambda), so a reading that is not the engine's own would group AND/OR
# around them differently: refused outside strings for that family. A `:`
# here is one not in a `::` cast. A `->` is refused wherever it stands,
# also inside a `->>`: DuckDB groups AND/OR into the left operand of both.
# Keyed by family, else by the renderer the family's parameters are
# written with.
_LOOSE_OPERATORS_BY_FAMILY = {
    "CosmosdbClient": ("?",),
    "PostHogClient": ("?", ":", "->"),
}
_LOOSE_OPERATORS_BY_RENDERER = {"duckdb": ("->",)}


@functools.lru_cache(maxsize=None)
def _loose_operators(client_type: Optional[str]) -> Tuple[str, ...]:
    """The operators `_outside_the_allow_list` refuses for the client on
    top of the shared allow-list: its family's, else its renderer's."""
    if client_type in _LOOSE_OPERATORS_BY_FAMILY:
        return _LOOSE_OPERATORS_BY_FAMILY[client_type]
    renderer = _renderer(client_type)
    return _LOOSE_OPERATORS_BY_RENDERER.get(renderer.name, ()) if renderer is not None else ()


def _is_control(char: str) -> bool:
    return char not in "\t\n" and unicodedata.category(char) == "Cc"


def _outside_the_allow_list(text: str, percent: bool, loose: Tuple[str, ...] = ()) -> Optional[str]:
    """None when `text` is plain enough that every reasonable reading draws
    the same string and name edges and finds no comment; else the kind of
    what is not (as in `structural_verdict`). One fixed reading: '...' is a
    string and '' inside it a quote; "..." is a name. `percent`: the engine
    may read `%` as a comment. `loose`: operators the engine reads as
    binding looser than AND/OR (`_loose_operators`)."""
    if "\\" in text:
        return "backslash"
    if any(_is_control(char) for char in text):
        return "char"
    code, i, n = [], 0, len(text)
    while i < n:
        char = text[i]
        if char == "'":
            i += 1
            while True:
                end = text.find("'", i)
                if end < 0:
                    return "unparseable"
                if text.startswith("''", end):
                    i = end + 2
                    continue
                i = end + 1
                break
            code.append(" ")
        elif char == '"':
            end = text.find('"', i + 1)
            if end < 0 or text.startswith('""', end) or not set(text[i + 1:end]) <= _ALLOWED_IN_NAMES:
                return "char"
            code.append(text[i:end + 1])
            i = end + 1
        elif char == "%" and percent:
            return "percent"
        elif char == ";":
            if text[i + 1:].strip(" \t\n"):
                return "char"
            i += 1
        elif char not in _ALLOWED_OUTSIDE_STRINGS:
            return "char"
        else:
            code.append(char)
            i += 1
    code_text = "".join(code)
    if any(sequence in code_text for sequence in _REFUSED_SEQUENCES):
        return "sequence"
    # A `::` cast is an operator of its own.
    others = code_text.replace("::", "  ")
    for operator in loose:
        if operator in others:
            return "char" if len(operator) == 1 else "sequence"
    return None


def _reading_of(client_type: Optional[str], literal_style: Optional[str]) -> str:
    """Which reading the identity check took of a capture, for the log."""
    renderer = _renderer(client_type)
    if renderer is not None and renderer.name == "ansi_or_backslash":
        return "standard and backslash-escape"
    if client_type in SOQL_CLIENTS:
        # Rewritten as standard literals and read in the generic dialect.
        return "generic"
    return _dialect_for(client_type, literal_style) or "generic"


def log_unverifiable(capture: str, client_type: Optional[str], param: str, reason: str) -> None:
    """Log that a filter was refused as syntax the check cannot verify for
    the client, by structure only: the client type, the param's name, the
    reason kind and the capture's length. Never the text or any value."""
    logger.warning(
        "identity_bind_check.unverifiable",
        extra={
            "event": "identity_bind_check_unverifiable",
            "client_type": client_type or "unknown",
            "param": param,
            "reason": reason,
            "query_length": len(capture),
        },
    )


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
