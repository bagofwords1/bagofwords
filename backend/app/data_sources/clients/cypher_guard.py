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


# Clauses that end a WHERE: after them a value no longer filters the rows.
_CLAUSE_WORDS = frozenset({
    "RETURN", "WITH", "ORDER", "SKIP", "LIMIT", "UNWIND", "MATCH", "OPTIONAL", "CALL", "UNION", "YIELD",
})


# Words after which "(" opens a pattern rather than a function call.
_BEFORE_PATTERN = frozenset({"MATCH", "OPTIONAL", "WHERE", "AND", "OR", "XOR", "NOT", "EXISTS"})


def cypher_binds(capture: str, needles: tuple[str, ...]) -> bool:
    """Whether a captured Neo4j query filters its rows by one of ``needles``
    (an identity value), for the identity-parameter check.

    The capture is the query text, or ``{"query", "params"}`` as JSON when it
    ran with parameters. A value counts only where it filters: in a WHERE
    clause or in a node / relationship property map, as ``$name`` bound to a
    needle or as the literal itself. A value that is merely passed, or used
    only in RETURN, does not."""
    import json

    query, params = capture, {}
    try:
        parsed = json.loads(capture)
        if isinstance(parsed, dict) and isinstance(parsed.get("query"), str):
            query, params = parsed["query"], parsed.get("params") or {}
    except (TypeError, ValueError):
        pass
    wanted = {str(n) for n in needles}

    def bound(key: str) -> bool:
        value = params.get(key)
        values = value if isinstance(value, list) else [value]
        return any(str(v) in wanted for v in values if v is not None)

    try:
        tokens = _tokens(query)
    except CypherNotAllowed:
        return False
    in_where = False
    stack: list[str] = []  # open brackets; "{p" marks a pattern property map
    for i, (kind, value) in enumerate(tokens):
        if kind == "word" and not _is_name_position(tokens, i):
            upper = value.upper()
            if upper == "WHERE":
                in_where = True
            elif upper in _CLAUSE_WORDS:
                in_where = False
        if kind == "sym" and value in "([{":
            prev = tokens[i - 1] if i > 0 else None
            if value == "(":
                # A function call's parenthesis, `count(`, is not a node pattern.
                call = prev is not None and prev[0] in ("word", "ident") and prev[1].upper() not in _BEFORE_PATTERN
                stack.append("f" if call else "(")
            elif value == "[":
                stack.append("[")
            else:
                # `(n:Label {p: …})` / `[r:TYPE {p: …}]`: a map that filters the pattern.
                in_pattern = bool(stack) and stack[-1] in ("(", "[")
                stack.append("{p" if in_pattern else "{")
            continue
        if kind == "sym" and value in ")]}":
            if stack:
                stack.pop()
            continue
        filtering = in_where or "{p" in stack
        if not filtering:
            continue
        if kind == "word" and i > 0 and tokens[i - 1] == ("sym", "$") and bound(value):
            return True
        if kind == "str" and value in wanted:
            return True
    return False
