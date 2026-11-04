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

# Unicode letters too: a namespace like ``myé.fn`` must not split into symbols.
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


def check_cypher(query: str | None) -> None:
    """Raise ``CypherNotAllowed`` unless ``query`` is read-only Cypher that
    stays in its database and calls only allowed procedures and functions."""
    if not isinstance(query, str) or not query.strip():
        return
    tokens = _tokens(query)
    i = 0
    while i < len(tokens):
        kind, value = tokens[i]
        if kind in ("sym", "str"):
            i += 1
            continue
        # A namespaced call, name.part(...), wherever a name starts a chain:
        # after a clause keyword, in a map value ({k: ns.fn()}), anywhere.
        # Only a word right after "." continues someone else's chain.
        prev = tokens[i - 1] if i > 0 else None
        if prev != ("sym", ".") and i + 1 < len(tokens) and tokens[i + 1] == ("sym", "."):
            name, end = _dotted_name(tokens, i)
            called = end < len(tokens) and tokens[end] == ("sym", "(") and "." in name
            if called and name.split(".", 1)[0] not in _ALLOWED_FUNCTION_NAMESPACES:
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
            if name not in _ALLOWED_PROCEDURES:
                raise CypherNotAllowed(
                    f"CALL {name or '…'} is not allowed. Only schema procedures can be called "
                    f"({', '.join(sorted(_ALLOWED_PROCEDURES))}); write the query in plain Cypher."
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
    """Whether a captured Neo4j query filters its rows by one of ``needles``
    (an identity value), for the identity-parameter check.

    The capture is the query text, or ``{"query", "params"}`` as JSON when it
    ran with parameters. The value counts only where it restricts the rows:
    - in a top-level WHERE right after MATCH or WITH, inside a conjunct
      (split on AND) with no OR / XOR / NOT, as ``$name`` bound to a needle
      or as the literal itself;
    - in a property map of a MATCH pattern, ``(n:Label {p: $name})``.
    Not in RETURN / WITH projections, list or pattern comprehensions, maps
    inside function calls, or after OPTIONAL MATCH. Closed by default: a
    shape it doesn't recognize does not count."""
    import json

    query, params = capture, {}
    try:
        parsed = json.loads(capture)
        if isinstance(parsed, dict) and isinstance(parsed.get("query"), str):
            query, params = parsed["query"], parsed.get("params") or {}
    except (TypeError, ValueError):
        pass
    wanted = {str(n) for n in needles}

    def is_value(tokens: list[Token], i: int) -> bool:
        kind, value = tokens[i]
        if kind == "str":
            return value in wanted
        if kind == "word" and i > 0 and tokens[i - 1] == ("sym", "$"):
            bound = params.get(value)
            values = bound if isinstance(bound, list) else [bound]
            return any(str(v) in wanted for v in values if v is not None)
        return False

    try:
        tokens = _tokens(query)
    except CypherNotAllowed:
        return False

    stack: list[str] = []  # "(" pattern, "f" call, "rel" relationship, "[" list, "{p" pattern map, "{" map
    clause, optional = "", False
    where: list[tuple[int, int]] = []  # (token index, depth) inside a filtering top-level WHERE
    in_where = False
    for i, (kind, value) in enumerate(tokens):
        # Clause keywords count only at the top level (not inside a subquery).
        if kind == "word" and not stack and not _is_name_position(tokens, i) and value.upper() in _CLAUSE_WORDS:
            upper = value.upper()
            if upper == "WHERE":
                in_where = clause == "WITH" or (clause == "MATCH" and not optional)
            else:
                in_where = False
                if upper == "OPTIONAL":
                    optional = True
                elif upper == "MATCH":
                    clause = "MATCH"
                else:
                    clause, optional = upper, False
            continue
        if kind == "sym" and value in "([{":
            prev = tokens[i - 1] if i > 0 else None
            if value == "(":
                call = prev is not None and prev[0] in ("word", "ident") and prev[1].upper() not in _BEFORE_PATTERN
                stack.append("f" if call else "(")
            elif value == "[":
                stack.append("rel" if prev in (("sym", "-"), ("sym", "<")) else "[")
            else:
                in_match_pattern = clause == "MATCH" and not optional and not in_where \
                    and bool(stack) and stack[-1] in ("(", "rel") and all(e in ("(", "rel") for e in stack)
                stack.append("{p" if in_match_pattern else "{")
            continue
        if kind == "sym" and value in ")]}":
            if stack:
                stack.pop()
            continue
        if stack and stack[-1] == "{p" and is_value(tokens, i):
            return True
        if in_where:
            where.append((i, len(stack)))

    # The WHERE regions, split into conjuncts on top-level AND.
    conjunct: list[int] = []
    conjuncts: list[list[int]] = []
    prev_index = None
    for i, depth in where:
        if prev_index is not None and i != prev_index + 1 and conjunct:
            conjuncts.append(conjunct)  # a new WHERE region
            conjunct = []
        prev_index = i
        kind, value = tokens[i]
        if depth == 0 and kind == "word" and value.upper() == "AND":
            conjuncts.append(conjunct)
            conjunct = []
            continue
        conjunct.append(i)
    if conjunct:
        conjuncts.append(conjunct)
    for part in conjuncts:
        if any(tokens[i][0] == "word" and tokens[i][1].upper() in _WEAKENING for i in part):
            continue
        if any(is_value(tokens, i) for i in part):
            return True
    return False
