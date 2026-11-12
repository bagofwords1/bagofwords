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

The code is read as Python (ast): comments, column names and other
identifiers never count as a reach, and a parameter list is a declaration,
not a reach. Python reads identifiers in NFKC form, and so does ast, so
`ds＿clients` is `ds_clients`.

The dict is handed to generate_df positionally (sandbox/namespace.py), so
its first parameter is a name for it whatever it is called, as are
`ds_clients` and the namespace global the run binds it to (CLIENTS_GLOBAL,
`db_clients`).

What this guarantees: it reads which clients good-faith step code reaches
-- code an LLM generated or a person wrote to get rows, not to slip past
this rule. Code written on purpose to evade it (evaluation built from
strings through pandas / numpy internals, and similar) is not covered; such
code still runs with its author's credentials, so it can only reach rows
those credentials already return. The cheap known ways around it are
closed below.

The rule is closed by default: the code counts as reaching only its literal
keys when every one of these holds, and as reaching ds_clients another way
otherwise --

- each use of a clients name is a literal-key subscript or `.get("<key>")`;
- generate_df is exactly one plain, undecorated, module-level def with a
  positional first parameter, and the name "generate_df" occurs nowhere
  else in the code (any node, any field -- a match capture, an except
  name, an import, a string);
- every import's root module is a library on DATA_LIBRARIES (no star or
  relative import; no internal pandas / numpy submodule -- core, testing,
  _private -- public ones like pandas.api.types are fine; `from functools`
  / `from typing` only the names listed);
- no name that reaches a variable by its name (locals(), getattr(), ...),
  no private attribute (`._rpc`) and no namespace-holding attribute;
- no string names a clients name; pandas' query()/eval() are called
  directly with a plain string with no `@` (they resolve @names from the
  caller's frame) -- a client's own .query(), the execute_query alias, is
  not pandas'; no `.core` / `.testing` attribute of pandas or numpy.
"""
from __future__ import annotations

import ast
import re
from typing import Iterable, NamedTuple, Optional

from app.ai.code_execution.sandbox.namespace import CLIENTS_GLOBAL

CLIENTS_NAME = "ds_clients"
# Every name the clients dict goes by before generate_df's own parameter.
CLIENT_DICT_NAMES = frozenset({CLIENTS_NAME, CLIENTS_GLOBAL})
# The function the dict is handed to, as its first positional argument.
ENTRY_FUNCTION = "generate_df"

# The modules step code may import and still count as reaching only its
# literal keys: what the coder prompt teaches and saved step code uses. Not a
# claim that they cannot reach an object by a string (pandas and numpy can,
# through their internals: see the module docstring). Anything else
# (operator, gc, string, types, weakref, copy, inspect, most of functools and
# typing -- get_type_hints evaluates names --, ...) counts as another reach:
# the conservative answer, every organization source.
DATA_LIBRARIES = {
    "pandas": "frames",
    "numpy": "arrays",
    "datetime": "dates",
    "zoneinfo": "timezones",
    "dateutil": "dates",
    "calendar": "dates",
    "time": "clock",
    "math": "arithmetic",
    "statistics": "statistics",
    "decimal": "numbers",
    "fractions": "numbers",
    "random": "sampling",
    "re": "text",
    "json": "text",
    "csv": "text",
    "io": "buffers",
    "textwrap": "text",
    "unicodedata": "text",
    "difflib": "text",
    "collections": "containers",
    "itertools": "iteration",
    "heapq": "ordering",
    "bisect": "ordering",
    "uuid": "ids",
    "hashlib": "hashing",
    "base64": "encoding",
    "sklearn": "models",
    "scipy": "models",
    "warnings": "silencing",
}
# Roots whose internal submodules (pandas.core, pandas._libs, numpy.testing,
# ...: a component on _INTERNAL_ATTRIBUTES or starting with "_") count as
# another reach; their public ones (pandas.api.types, numpy.linalg) do not.
_ROOT_ONLY = frozenset({"pandas", "numpy"})
# The run's namespace binds pandas and numpy under these names.
_NAMESPACE_LIBRARY_NAMES = frozenset({"pd", "np"})
# Names that may be imported from a module that is otherwise off the list.
_IMPORTABLE_NAMES = {
    "functools": frozenset({"reduce", "partial"}),
    "typing": frozenset({"Any", "Dict", "List", "Optional", "Tuple", "Union", "Iterable", "Sequence",
                         "Mapping", "Callable", "Set"}),
}
# Attributes of pandas / numpy that lead into their internals.
_INTERNAL_ATTRIBUTES = frozenset({"core", "testing", "ctypeslib", "f2py"})
# Names that reach a variable by its name, or the run's namespace.
_NAMESPACE_NAMES = frozenset({
    "locals", "globals", "vars", "eval", "exec", "compile", "getattr", "setattr", "delattr",
    "hasattr", "__import__", "__builtins__", "dir",
})
# Attributes that hold a namespace or raw memory, beside every private one.
_NAMESPACE_ATTRIBUTES = frozenset({
    "f_locals", "f_globals", "f_back", "f_builtins", "gi_frame", "cr_frame", "ag_frame",
    "tb_frame", "ctypes", "ctypeslib",
})
_ALLOWED_PRIVATE_ATTRIBUTES = frozenset({"__name__"})
# pandas methods that resolve @names from the caller's frame.
_FRAME_EVALUATORS = frozenset({"eval", "query"})

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


def _str_fields(node):
    """Every str an AST node carries in its own fields (names, identifiers,
    string constants)."""
    for _, value in ast.iter_fields(node):
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    yield item


def _entry_parameter(tree) -> tuple[Optional[str], bool]:
    """(generate_df's first parameter, whether generate_df is one plain
    function the run hands the dict to). True with None when the name does
    not occur at all (a fragment: nothing else receives the dict)."""
    occurrences = sum(1 for node in ast.walk(tree) for s in _str_fields(node) if s == ENTRY_FUNCTION)
    if occurrences == 0:
        return None, True
    defs = [n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == ENTRY_FUNCTION]
    if occurrences != 1 or len(defs) != 1 or defs[0].decorator_list:
        return None, False
    positional = list(defs[0].args.posonlyargs) + list(defs[0].args.args)
    if not positional:
        return None, False
    return positional[0].arg, True


def _internal_component(name: str) -> bool:
    return name in _INTERNAL_ATTRIBUTES or name.startswith("_")


def _module_off_the_allowlist(module: str) -> bool:
    root, *rest = module.split(".")
    return root not in DATA_LIBRARIES or (root in _ROOT_ONLY and any(_internal_component(c) for c in rest))


def _import_off_the_allowlist(node) -> bool:
    if isinstance(node, ast.Import):
        return any(_module_off_the_allowlist(a.name) for a in node.names)
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if node.level != 0 or any(a.name == "*" for a in node.names):
            return True
        if module in _IMPORTABLE_NAMES:
            return any(a.name not in _IMPORTABLE_NAMES[module] for a in node.names)
        if module.split(".")[0] in _ROOT_ONLY and any(
                a.name in _FRAME_EVALUATORS or _internal_component(a.name) for a in node.names):
            return True  # pandas.eval / query under a name of the code's choosing, or an internal
        return _module_off_the_allowlist(module)
    return False


def _chain_base(node):
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _library_names(tree) -> frozenset:
    """Names pandas and numpy (and their submodules) go by in the code."""
    names = set(_NAMESPACE_LIBRARY_NAMES)
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] in _ROOT_ONLY:
                    names.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in _ROOT_ONLY:
            names.update(a.asname or a.name for a in n.names)
    return frozenset(names)


def _bound_names(tree) -> dict:
    """How many times each name is bound, by any binding form: assignment
    targets, parameters, except clauses, match captures, imports, defs and
    classes."""
    counts: dict[str, int] = {}

    def bind(name):
        if name:
            counts[name] = counts.get(name, 0) + 1

    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
            bind(n.id)
        elif isinstance(n, ast.arg):
            bind(n.arg)
        elif isinstance(n, ast.ExceptHandler):
            bind(n.name)
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)):
            bind(n.name)
        elif isinstance(n, ast.MatchMapping):
            bind(n.rest)
        elif isinstance(n, ast.alias):
            bind(n.asname or n.name.split(".")[0])
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bind(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            for name in n.names:
                bind(name)
    return counts


def _reaches_by_name(node, mentions, client_receivers: set, evaluator_calls: set,
                     library_names: frozenset) -> bool:
    if isinstance(node, ast.Name):
        return node.id in _NAMESPACE_NAMES
    if isinstance(node, ast.Attribute):
        if node.attr in _FRAME_EVALUATORS and id(node.value) not in client_receivers:
            # pandas' query()/eval() resolve @names from the caller's frame:
            # only a direct call with a plain string (checked below) is read.
            if id(node) not in evaluator_calls:
                return True
        if node.attr in _INTERNAL_ATTRIBUTES and _chain_base(node.value) in library_names:
            return True  # pandas / numpy internals (pd.core, np.testing)
        return node.attr in _NAMESPACE_ATTRIBUTES or (
            node.attr.startswith("_") and node.attr not in _ALLOWED_PRIVATE_ATTRIBUTES)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return bool(mentions.search(node.value))
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _FRAME_EVALUATORS
            and id(node.func.value) not in client_receivers):
        first = node.args[0] if node.args else None
        return not (isinstance(first, ast.Constant) and isinstance(first.value, str)
                    and "@" not in first.value) or any(k.arg in ("local_dict", "global_dict", "resolvers")
                                                         for k in node.keywords)
    return _import_off_the_allowlist(node)


def _literal(node) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def client_reach(code: Optional[str]) -> ClientReach:
    """The literal keys `code` uses on ds_clients, and whether it reaches
    ds_clients any other way (see the module docstring)."""
    if not code or not code.strip():
        return _NO_REACH
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return _UNREADABLE

    parameter, plain_entry = _entry_parameter(tree)
    names = CLIENT_DICT_NAMES | ({parameter} if parameter else set())
    mentions = re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(n) for n in sorted(names))
                          + r")(?![A-Za-z0-9_])")

    def _is_clients(node) -> bool:
        return isinstance(node, ast.Name) and node.id in names

    keys: list[str] = []
    keyed: set[int] = set()  # ids of the Name nodes a literal-key access accounts for
    key_constants: set[int] = set()  # the key strings themselves (a key may name a client)
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_clients(node.value):
            key = _literal(node.slice)
            if key is not None:
                keys.append(key)
                keyed.add(id(node.value))
                key_constants.add(id(node.slice))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "get" and _is_clients(node.func.value) and node.args):
            key = _literal(node.args[0])
            if key is not None:
                keys.append(key)
                keyed.add(id(node.func.value))
                key_constants.add(id(node.args[0]))
    # A client's own .query() -- the alias of execute_query -- is not pandas':
    # its receiver is a literal-key access, or a name bound only to one.
    accesses = {id(n) for n in ast.walk(tree)
                if (isinstance(n, ast.Subscript) and id(n.value) in keyed)
                or (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and id(n.func.value) in keyed)}
    bindings = _bound_names(tree)
    client_names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                and id(n.value) in accesses and bindings.get(n.targets[0].id, 0) == 1:
            client_names.add(n.targets[0].id)
    client_receivers = set(accesses) | {
        id(n) for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        and n.id in client_names}
    library_names = _library_names(tree)
    evaluator_calls = {id(n.func) for n in ast.walk(tree)
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}

    other = not plain_entry or any(
        (_is_clients(node) and id(node) not in keyed)
        or (id(node) not in key_constants
            and _reaches_by_name(node, mentions, client_receivers, evaluator_calls, library_names))
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
