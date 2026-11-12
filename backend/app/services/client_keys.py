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
its first parameter is a name for it whatever it is called, as are
`ds_clients` and the namespace global the run binds it to (CLIENTS_GLOBAL,
`db_clients`). Any use of such a name other than a literal-key subscript or
`.get("<key>")` -- passing it to a function, assigning it, iterating it,
another attribute -- is another reach. So is anything that reaches a
variable by its name (locals(), globals(), vars(), eval(), exec(),
compile(), getattr(), __import__, __builtins__, sys, inspect, a frame's or
function's globals), and a generate_df that is not exactly one plain,
undecorated, module-level def whose first parameter is positional: then what
receives the dict is not known.
"""
from __future__ import annotations

import ast
from typing import Iterable, NamedTuple, Optional

from app.ai.code_execution.sandbox.namespace import CLIENTS_GLOBAL

CLIENTS_NAME = "ds_clients"
# Every name the clients dict goes by before generate_df's own parameter.
CLIENT_DICT_NAMES = frozenset({CLIENTS_NAME, CLIENTS_GLOBAL})
# The function the dict is handed to, as its first positional argument.
ENTRY_FUNCTION = "generate_df"
# Names that reach a variable by its name, or the run's namespace.
_NAMESPACE_NAMES = frozenset({
    "locals", "globals", "vars", "eval", "exec", "compile", "getattr", "__import__",
    "__builtins__", "sys", "inspect", "importlib", "builtins",
})
# Attributes that hold a namespace: a frame's, a function's, a module's.
_NAMESPACE_ATTRIBUTES = frozenset({
    "f_locals", "f_globals", "f_back", "f_builtins", "__globals__", "__dict__", "__builtins__",
    "gi_frame", "cr_frame", "ag_frame", "tb_frame", "__closure__",
})

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


def _binds_entry_function(node) -> bool:
    """Whether `node` binds the name generate_df (other than by a def)."""
    if isinstance(node, ast.Name):
        return node.id == ENTRY_FUNCTION and not isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.ClassDef):
        return node.name == ENTRY_FUNCTION
    if isinstance(node, ast.alias):
        return (node.asname or node.name.split(".")[0]) == ENTRY_FUNCTION
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return ENTRY_FUNCTION in node.names
    if isinstance(node, ast.arg):
        return node.arg == ENTRY_FUNCTION
    return False


def _entry_parameter(tree) -> tuple[Optional[str], bool]:
    """(generate_df's first parameter, whether generate_df is one plain
    function the run hands the dict to). True with None when the code has no
    generate_df at all (a fragment: nothing else receives the dict)."""
    defs = [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == ENTRY_FUNCTION]
    if any(_binds_entry_function(n) for n in ast.walk(tree)):
        return None, False
    if not defs:
        return None, True
    if len(defs) != 1 or defs[0] not in tree.body or defs[0].decorator_list:
        return None, False
    positional = list(defs[0].args.posonlyargs) + list(defs[0].args.args)
    if not positional:
        return None, False
    return positional[0].arg, True


def _reaches_namespace(node) -> bool:
    if isinstance(node, ast.Name):
        return node.id in _NAMESPACE_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in _NAMESPACE_ATTRIBUTES
    if isinstance(node, ast.alias):
        return node.name.split(".")[0] in _NAMESPACE_NAMES
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").split(".")[0] in _NAMESPACE_NAMES
    return False


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

    parameter, plain_entry = _entry_parameter(tree)
    names = CLIENT_DICT_NAMES | ({parameter} if parameter else set())

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
    other = not plain_entry or any(
        (_is_clients(node) and id(node) not in keyed) or _reaches_namespace(node)
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
