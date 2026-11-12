"""Which data-source clients a step's code reaches.

Generated code reaches a source through the client dict it is handed:
`ds_clients["<source>:<connection>"]` (or `.get(...)` with the same literal
key). construct_clients (DataSourceService) builds those keys:
"<source>:<connection>", "<source>:<connection>::fast" for the accelerated
relations, and the legacy alias "<source>" on a single-connection source.

This module answers the two questions every rule about "the sources a step
uses" asks of the code, so they cannot drift apart:

- which literal keys the code uses (client_reach / literal_client_keys), and
  whether it also reaches the dict some other way -- a computed key, an alias,
  iteration, `.values()` / `.items()`, passing it to a function -- in which
  case the literal keys do not tell which sources it reaches;
- which organization sources those keys name (sources_for_client_keys).

The code is read as Python (ast): comments, string contents, column names
and aliases never count as a reach, and a parameter list is a declaration,
not a reach. Python reads identifiers in NFKC form, and so does ast, so
`ds＿clients` is `ds_clients`.

The dict is handed to generate_df positionally (sandbox/namespace.py), so
its first parameter is a name for it whatever it is called, as is
`ds_clients`. Any use of such a name other than a literal-key subscript or
`.get("<key>")` -- passing it to a function, assigning it, iterating it,
another attribute -- is another reach, and so is any use of locals(),
globals(), vars(), eval(), exec() or compile(), which can reach it by name.
"""
from __future__ import annotations

import ast
from typing import Iterable, NamedTuple, Optional

CLIENTS_NAME = "ds_clients"
# The function the dict is handed to, as its first positional argument.
ENTRY_FUNCTION = "generate_df"
# Builtins that reach a variable by its name.
_NAMESPACE_BUILTINS = frozenset({"locals", "globals", "vars", "eval", "exec", "compile"})

# Keys of ds_clients that are not an organization data source: the platform's
# own client (app/data_sources/clients/bow_client.py, clients["bow"]).
PLATFORM_CLIENT_KEYS = frozenset({"bow"})


class ClientReach(NamedTuple):
    """How a piece of code reaches ds_clients."""

    # Literal keys, in the order the code uses them (repeats kept).
    keys: tuple
    # True when the code also reaches ds_clients in a way a literal key does
    # not describe -- or cannot be read as Python at all.
    other: bool

    @property
    def mentioned(self) -> bool:
        return bool(self.keys) or self.other


_NO_REACH = ClientReach((), False)
_UNREADABLE = ClientReach((), True)


def _clients_names(tree) -> frozenset:
    """`ds_clients` and the first positional parameter of every generate_df."""
    names = {CLIENTS_NAME}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == ENTRY_FUNCTION:
            positional = list(node.args.posonlyargs) + list(node.args.args)
            if positional:
                names.add(positional[0].arg)
    return frozenset(names)


def _literal(node) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def client_reach(code: Optional[str]) -> ClientReach:
    """The literal keys `code` uses on ds_clients, and whether it reaches
    ds_clients any other way (see ClientReach)."""
    if not code or not code.strip():
        return _NO_REACH
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return _UNREADABLE

    names = _clients_names(tree)

    def _is_clients(node) -> bool:
        return isinstance(node, ast.Name) and node.id in names

    keys: list[str] = []
    keyed: set[int] = set()  # ids of the Name nodes a literal-key access accounts for
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_clients(node.value):
            key = _literal(node.slice)
            if key is not None:
                keys.append(key)
                keyed.add(id(node.value))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "get" and _is_clients(node.func.value) and node.args):
            key = _literal(node.args[0])
            if key is not None:
                keys.append(key)
                keyed.add(id(node.func.value))
    other = any(
        (_is_clients(node) and id(node) not in keyed)
        or (isinstance(node, ast.Name) and node.id in _NAMESPACE_BUILTINS)
        for node in ast.walk(tree)
    )
    return ClientReach(tuple(keys), other)


def literal_client_keys(code: Optional[str]) -> Optional[list[str]]:
    """The literal keys `code` uses on ds_clients ([] when it never mentions
    ds_clients), or None when it reaches ds_clients any other way -- then the
    keys do not tell which clients it reaches."""
    reach = client_reach(code)
    return None if reach.other else list(reach.keys)


def source_names_for_key(names: Iterable[str], key: str) -> list[str]:
    """Every source name a client key may belong to: the key itself (the
    legacy single-connection alias) or a name the key starts with followed by
    ":" ("<source>:<connection>", "<source>:<connection>::fast"). All of them:
    "Sales:EU:pg" is source "Sales:EU" on connection "pg" or source "Sales" on
    connection "EU:pg", and a check that refuses must count both. [] when no
    source matches."""
    return [name for name in names
            if name and (key == name or key.startswith(name + ":"))]


def sources_for_client_keys(rows: list, keys: Iterable[str]) -> tuple[list[str], list[str]]:
    """(ids of the (id, name) `rows` the keys name, keys that name none).
    Platform client keys (PLATFORM_CLIENT_KEYS) name no source and are in
    neither list unless an organization source carries that name."""
    names = [str(r[1]) for r in rows if r[1]]
    ids_by_name: dict[str, list[str]] = {}
    for r in rows:
        if r[1]:
            ids_by_name.setdefault(str(r[1]), []).append(str(r[0]))
    picked: list[str] = []
    unmatched: list[str] = []
    for key in keys:
        matched = source_names_for_key(names, key)
        if not matched:
            if key not in PLATFORM_CLIENT_KEYS:
                unmatched.append(key)
            continue
        for name in matched:
            for ds_id in ids_by_name[name]:
                if ds_id not in picked:
                    picked.append(ds_id)
    return picked, unmatched
