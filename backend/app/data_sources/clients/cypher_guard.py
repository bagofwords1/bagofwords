"""Read-only check for agent-written Cypher (the Neo4j connector).

Defense in depth on top of the READ session the client opens: the server
refuses writes there, but a READ session does not stop the server from reading
URLs itself (``LOAD CSV FROM 'http://…'``, APOC/GenAI procedures), switching
database (``USE``), or running admin commands. So this check is closed by
default: write and admin clauses are refused, and every procedure (``CALL x``)
and namespaced function (``x.y(...)``) must be on an allow-list.

The lexer knows Cypher's quoting: ``'…'`` and ``"…"`` with backslash escapes,
`` `identifiers` ``, ``//`` and ``/* */`` comments. Keywords inside them don't
count, and a word right after ``.``, ``:`` or ``$``, or a map key, is a
property, label, relationship type or parameter, not a clause.
"""
from __future__ import annotations

import re


class CypherNotAllowed(ValueError):
    """The query would write, leave its database, or reach outside it."""


# Clauses that write, administer, or change what the query reads.
_REFUSED_WORDS = frozenset({
    "CREATE", "MERGE", "DELETE", "DETACH", "NODETACH", "SET", "REMOVE", "DROP", "FOREACH", "INSERT",
    "ALTER", "GRANT", "DENY", "REVOKE", "RENAME", "START", "STOP", "TERMINATE",
    "ENABLE", "DEALLOCATE", "REALLOCATE", "USE", "SHOW", "LOAD",
})

# Procedures the agent may CALL: schema introspection and version only.
_ALLOWED_PROCEDURES = frozenset({
    "db.labels", "db.relationshiptypes", "db.propertykeys", "db.indexes",
    "db.schema.visualization", "db.schema.nodetypeproperties", "db.schema.reltypeproperties",
    "dbms.components",
})

# Namespaces of Cypher's own dotted functions (date.truncate, point.distance…).
# Anything else dotted and called (apoc.*, gds.*, genai.*, user functions)
# can run Cypher, write, or call out of the server.
_ALLOWED_FUNCTION_NAMESPACES = frozenset({
    "date", "datetime", "localdatetime", "localtime", "time", "duration", "point", "vector",
})

# Graph Data Science, when the connection allows it. GDS works on in-memory
# projections, so a projection and its cleanup are allowed alongside the
# algorithms' stream / stats modes (and their memory estimates). write and
# mutate modes, exports and Cypher-string projections (whose inner queries
# this check cannot see) stay refused.
_GDS_PROCEDURES = frozenset({
    "gds.version", "gds.list", "gds.graph.list", "gds.graph.exists", "gds.graph.drop",
    "gds.graph.project", "gds.graph.project.estimate",
})
_GDS_MODES = ("stream", "stats", "estimate")
_GDS_REFUSED_PARTS = frozenset({"write", "mutate", "export", "cypher", "csv", "subgraph", "filter", "generate"})
# Functions: the aggregation-form projection and node lookups.
_GDS_FUNCTIONS = frozenset({"gds.graph.project", "gds.util.asnode", "gds.util.asnodes", "gds.version"})


def _gds_procedure_allowed(name: str) -> bool:
    if name in _GDS_PROCEDURES:
        return True
    parts = name.split(".")
    if len(parts) < 3 or parts[0] != "gds" or _GDS_REFUSED_PARTS.intersection(parts):
        return False
    return parts[-1] in _GDS_MODES


def _function_allowed(name: str, allow_gds: bool) -> bool:
    """Whether the namespaced function ``name`` (lowercased) may be called."""
    if name.split(".", 1)[0] in _ALLOWED_FUNCTION_NAMESPACES:
        return True
    return allow_gds and name in _GDS_FUNCTIONS


def _procedure_allowed(name: str, allow_gds: bool) -> bool:
    return name in _ALLOWED_PROCEDURES or (allow_gds and _gds_procedure_allowed(name))


# Unicode letters too: a namespace like ``myé.fn`` must not split into symbols.
# Line breaks other than \n / \r: refused, so the guard never has to agree
# with the server on where a // comment ends.
_OTHER_LINE_BREAKS = ("\u0085", "\u2028", "\u2029", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e")

_WORD = re.compile(r"[^\W\d]\w*")

Token = tuple[str, str]  # (kind, value): "word", "ident" (backticked), "str" (a literal's text) or "sym"


def _tokens(query: str) -> list[Token]:
    out: list[Token] = []
    i, n = 0, len(query)
    while i < n:
        c = query[i]
        if c.isspace():
            i += 1
        elif query.startswith("//", i):
            # Neo4j ends a line comment at \r as well as \n: so must we, or
            # what follows a lone \r would pass as comment and still run.
            ends = [j for j in (query.find("\n", i), query.find("\r", i)) if j >= 0]
            i = min(ends) + 1 if ends else n
        elif query.startswith("/*", i):
            j = query.find("*/", i + 2)
            if j < 0:
                raise CypherNotAllowed("The query has an unterminated /* comment.")
            i = j + 2
        elif c in "'\"":
            j = i + 1
            while j < n and query[j] != c:
                j += 2 if query[j] == "\\" else 1
            if j >= n:
                raise CypherNotAllowed("The query has an unterminated string.")
            out.append(("str", query[i + 1:j]))  # a literal: a value, never a clause
            i = j + 1
        elif c == "`":
            j = i + 1
            name = []
            while True:
                if j >= n:
                    raise CypherNotAllowed("The query has an unterminated `identifier`.")
                if query[j] == "`":
                    if j + 1 < n and query[j + 1] == "`":  # `` escapes a backtick
                        name.append("`")
                        j += 2
                        continue
                    break
                name.append(query[j])
                j += 1
            out.append(("ident", "".join(name)))
            i = j + 1
        else:
            m = _WORD.match(query, i)
            if m:
                out.append(("word", m.group(0)))
                i = m.end()
            else:
                out.append(("sym", c))
                i += 1
    return out


def _dotted_name(tokens: list[Token], start: int) -> tuple[str, int]:
    """The name ``a.b.c`` (words or backticked parts) starting at ``start``,
    lowercased, and the index after it."""
    parts: list[str] = []
    i = start
    while i < len(tokens) and tokens[i][0] in ("word", "ident"):
        parts.append(tokens[i][1])
        if i + 2 < len(tokens) and tokens[i + 1] == ("sym", ".") and tokens[i + 2][0] in ("word", "ident"):
            i += 2
            continue
        i += 1
        break
    return ".".join(parts).lower(), i


def _is_name_position(tokens: list[Token], i: int) -> bool:
    """A word that names something rather than starting a clause: a property
    (``n.set``), a label or type (``:Delete``), a parameter (``$use``), or a
    map key (``{set: 1}``)."""
    prev = tokens[i - 1] if i > 0 else None
    if prev in (("sym", "."), ("sym", ":"), ("sym", "$")):
        return True
    nxt = tokens[i + 1] if i + 1 < len(tokens) else None
    return nxt == ("sym", ":") and prev in (("sym", "{"), ("sym", ","))


def check_cypher(query: str | None, *, allow_gds: bool = False) -> None:
    """Raise ``CypherNotAllowed`` unless ``query`` is read-only Cypher that
    stays in its database and calls only allowed procedures and functions.
    ``allow_gds`` adds the read-only Graph Data Science calls."""
    if not isinstance(query, str) or not query.strip():
        return
    if any(c in query for c in _OTHER_LINE_BREAKS):
        raise CypherNotAllowed("The query contains an unusual line break; use \\n.")
    tokens = _tokens(query)
    i = 0
    while i < len(tokens):
        kind, value = tokens[i]
        if kind in ("sym", "str"):
            i += 1
            continue
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        # `apoc.text.join`(...): one backticked name holding a namespace.
        if kind == "ident" and "." in value and nxt == ("sym", "(") \
                and not _function_allowed(value.lower(), allow_gds):
            raise CypherNotAllowed(f"The function {value}() is not allowed on this connection.")
        # An alias (`AS start`) or a variable (`stop.x`) is a name, not a clause.
        prev_word = tokens[i - 1] if i > 0 else None
        # Names, not clauses: an alias (`AS start`), a variable in a pattern
        # (`(stop:Event)`), or one read with a dot (`set.name`). No clause can
        # stand in any of these places.
        alias = prev_word is not None and prev_word[0] == "word" and prev_word[1].upper() == "AS"
        variable = prev_word == ("sym", "(") or nxt == ("sym", ".")
        if kind == "word" and (alias or variable) and nxt != ("sym", "."):
            i += 1
            continue
        if kind == "word" and nxt == ("sym", ".") and value.upper() in _REFUSED_WORDS:
            # `set.name`: still check a namespaced call, but never as a clause.
            name, end = _dotted_name(tokens, i)
            if end < len(tokens) and tokens[end] == ("sym", "(") \
                    and not _function_allowed(name, allow_gds):
                raise CypherNotAllowed(f"The function {name}() is not allowed on this connection.")
            i = end
            continue
        # A namespaced call, name.part(...), wherever a name starts a chain:
        # after a clause keyword, in a map value ({k: ns.fn()}), anywhere.
        # Only a word right after "." continues someone else's chain.
        prev = tokens[i - 1] if i > 0 else None
        if prev != ("sym", ".") and i + 1 < len(tokens) and tokens[i + 1] == ("sym", "."):
            name, end = _dotted_name(tokens, i)
            called = end < len(tokens) and tokens[end] == ("sym", "(") and "." in name
            if called and not _function_allowed(name, allow_gds):
                raise CypherNotAllowed(
                    f"The function {name}() is not allowed on this connection. "
                    "Use Cypher's built-in functions."
                )
        if _is_name_position(tokens, i):
            i += 1
            continue
        # A backticked name is never a keyword, but can start a namespace.
        upper = value.upper() if kind == "word" else ""
        if upper in _REFUSED_WORDS:
            raise CypherNotAllowed(
                f"'{upper}' is not allowed: this connection is read-only and stays in its database. "
                "Use MATCH … RETURN to read."
            )
        # IN TRANSACTIONS, IN CONCURRENT TRANSACTIONS, IN 4 CONCURRENT TRANSACTIONS
        if upper == "TRANSACTIONS" and any(t[0] == "word" and t[1].upper() == "IN" for t in tokens[max(0, i - 4):i]):
            raise CypherNotAllowed("CALL { … } IN TRANSACTIONS is not allowed on a read-only connection.")
        if upper == "CALL":
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if nxt == ("sym", "{") or nxt == ("sym", "("):
                i += 1  # a subquery: its body is checked like the rest
                continue
            name, end = _dotted_name(tokens, i + 1)
            if not _procedure_allowed(name, allow_gds):
                gds = (" and GDS algorithms in stream / stats mode (gds.<algorithm>.stream)"
                       if allow_gds else "")
                raise CypherNotAllowed(
                    f"CALL {name or '…'} is not allowed. Only schema procedures can be called "
                    f"({', '.join(sorted(_ALLOWED_PROCEDURES))}){gds}; write the query in plain Cypher."
                )
            i = end
            continue
        i += 1


# Top-level clauses. A WHERE filters the rows only right after MATCH or WITH;
# after OPTIONAL MATCH it only decides what the optional part matches.
_CLAUSE_WORDS = frozenset({
    "MATCH", "OPTIONAL", "WITH", "RETURN", "UNWIND", "ORDER", "SKIP", "LIMIT", "CALL", "UNION", "YIELD", "WHERE",
})
# Words after which "(" opens a pattern rather than a function call.
_BEFORE_PATTERN = frozenset({"MATCH", "OPTIONAL", "WHERE", "AND", "OR", "XOR", "NOT", "EXISTS"})
# A conjunct holding one of these can be true without the identity value.
_WEAKENING = frozenset({"OR", "XOR", "NOT"})


def cypher_binds(capture: str, needles: tuple[str, ...]) -> bool:
    """Whether a captured Neo4j query restricts its rows to one of ``needles``
    (an identity value), for the identity-parameter check.

    The capture is the query text, or ``{"query", "params"}`` as JSON when it
    ran with parameters. Closed by default; the query counts only when every
    ``UNION`` branch has exactly one MATCH (no OPTIONAL MATCH) and that branch
    compares a property to the value by ``=`` or ``IN``:
    - ``WHERE n.p = $v`` (or ``$v = n.p``, ``n.p IN $v``) in a top-level WHERE
      after the MATCH or a WITH, in an AND-conjunct with no OR / XOR / NOT;
    - ``(n:Label {p: $v})`` in the MATCH pattern.
    ``$v`` is a parameter bound to a needle, or the literal itself."""
    import json

    query, params = capture, {}
    try:
        parsed = json.loads(capture)
        if isinstance(parsed, dict) and isinstance(parsed.get("query"), str):
            query, params = parsed["query"], parsed.get("params") or {}
    except (TypeError, ValueError):
        pass
    wanted = {str(n) for n in needles}
    try:
        tokens = _tokens(query)
    except CypherNotAllowed:
        return False

    def value_at(i: int) -> int:
        """Length of an identity value starting at i ($name or a literal), else 0."""
        if i >= len(tokens):
            return 0
        kind, value = tokens[i]
        if kind == "str":
            return 1 if value in wanted else 0
        if tokens[i] == ("sym", "$") and i + 1 < len(tokens) and tokens[i + 1][0] == "word":
            bound = params.get(tokens[i + 1][1])
            values = bound if isinstance(bound, list) else [bound]
            return 2 if any(str(v) in wanted for v in values if v is not None) else 0
        return 0

    def prop_at(i: int) -> bool:  # n.p
        return i + 2 < len(tokens) and tokens[i][0] in ("word", "ident") and tokens[i + 1] == ("sym", ".") \
            and tokens[i + 2][0] in ("word", "ident")

    def compares(part: list[int]) -> bool:
        seq = [tokens[i] for i in part]
        idx = part
        for k in range(len(seq)):
            i = idx[k]
            # n.p = v | n.p IN v
            if prop_at(i) and k + 3 < len(seq) and idx[k + 3] == i + 3:
                op = tokens[i + 3]
                if (op == ("sym", "=") or (op[0] == "word" and op[1].upper() == "IN")) and value_at(i + 4):
                    return True
            # v = n.p
            n = value_at(i)
            if n and i + n < len(tokens) and tokens[i + n] == ("sym", "=") and prop_at(i + n + 1):
                return True
        return False

    # Split into UNION branches (top level).
    branches: list[list[int]] = [[]]
    depth = 0
    for i, (kind, value) in enumerate(tokens):
        if kind == "sym" and value in "([{":
            depth += 1
        elif kind == "sym" and value in ")]}":
            depth -= 1
        if depth == 0 and kind == "word" and value.upper() == "UNION":
            branches.append([])
            continue
        branches[-1].append(i)

    def branch_binds(idx: list[int]) -> bool:
        words = [tokens[i][1].upper() for i in idx if tokens[i][0] == "word" and not _is_name_position(tokens, i)]
        if words.count("MATCH") != 1 or "OPTIONAL" in words or "CALL" in words:
            return False
        stack: list[str] = []
        clause, in_where = "", False
        where: list[tuple[int, int]] = []
        for i in idx:
            kind, value = tokens[i]
            if kind == "word" and not stack and not _is_name_position(tokens, i) and value.upper() in _CLAUSE_WORDS:
                upper = value.upper()
                if upper == "WHERE":
                    in_where = clause in ("MATCH", "WITH")
                else:
                    in_where, clause = False, upper
                continue
            if in_where:
                where.append((i, len(stack)))
            if kind == "sym" and value in "([{":
                if value == "{" and clause == "MATCH" and not in_where and stack and stack[-1] in ("(", "rel"):
                    # A MATCH pattern map: {p: v} compares p to v.
                    j = i + 1
                    while j < len(tokens) and tokens[j] != ("sym", "}"):
                        if tokens[j][0] in ("word", "ident") and j + 1 < len(tokens) \
                                and tokens[j + 1] == ("sym", ":") and value_at(j + 2):
                            return True
                        j += 1
                prev = tokens[i - 1] if i > 0 else None
                if value == "(":
                    call = prev is not None and prev[0] in ("word", "ident") and prev[1].upper() not in _BEFORE_PATTERN
                    stack.append("f" if call else "(")
                elif value == "[":
                    stack.append("rel" if prev in (("sym", "-"), ("sym", "<")) else "[")
                else:
                    stack.append("{")
            elif kind == "sym" and value in ")]}" and stack:
                stack.pop()
        # Conjuncts: split on AND at the WHERE's own depth.
        parts: list[list[int]] = [[]]
        for i, d in where:
            if d == 0 and tokens[i][0] == "word" and tokens[i][1].upper() == "AND":
                parts.append([])
                continue
            parts[-1].append(i)
        for part in parts:
            if any(tokens[i][0] == "word" and tokens[i][1].upper() in _WEAKENING for i in part):
                continue
            if compares(part):
                return True
        return False

    return all(branch_binds(b) for b in branches if b)
