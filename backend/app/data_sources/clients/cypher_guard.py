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

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

Token = tuple[str, str]  # (kind, value): kind is "word", "ident" (backticked) or "sym"


def _tokens(query: str) -> list[Token]:
    out: list[Token] = []
    i, n = 0, len(query)
    while i < n:
        c = query[i]
        if c.isspace():
            i += 1
        elif query.startswith("//", i):
            j = query.find("\n", i)
            i = n if j < 0 else j + 1
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
            out.append(("sym", "''"))  # a literal: a value, never a clause
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
        if kind == "sym" or _is_name_position(tokens, i):
            i += 1
            continue
        # A backticked name is never a keyword, but can start a namespace.
        upper = value.upper() if kind == "word" else ""
        if upper in _REFUSED_WORDS:
            raise CypherNotAllowed(
                f"'{upper}' is not allowed: this connection is read-only and stays in its database. "
                "Use MATCH … RETURN to read."
            )
        if upper == "TRANSACTIONS" and i > 0 and tokens[i - 1][0] == "word" and tokens[i - 1][1].upper() == "IN":
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
        # A namespaced function call: name.part(...)
        if i + 1 < len(tokens) and tokens[i + 1] == ("sym", "."):
            name, end = _dotted_name(tokens, i)
            called = end < len(tokens) and tokens[end] == ("sym", "(") and "." in name
            if called and name.split(".", 1)[0] not in _ALLOWED_FUNCTION_NAMESPACES:
                raise CypherNotAllowed(
                    f"The function {name}() is not allowed on this connection. "
                    "Use Cypher's built-in functions."
                )
            i = end
            continue
        i += 1
