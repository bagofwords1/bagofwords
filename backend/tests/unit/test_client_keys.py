"""Which data-source clients a step's code reaches (app/services/client_keys.py).

One rule for every question about "the sources a step uses": the creator-mode
and withholding policy, the share-dialog flags, fork copying and eligibility,
and fork hydration's repairable check. A source is reached by its client key
(`ds_clients["<source>:<connection>"]`); reaching ds_clients any other way
means the keys do not tell.
"""
import pytest

from app.services.client_keys import (
    client_reach,
    literal_client_keys,
    source_names_for_key,
    sources_for_client_keys,
)


@pytest.mark.parametrize("code, expected", [
    ('ds_clients["Sales:pg"].execute_query("x")', ["Sales:pg"]),
    ("ds_clients.get('Sales:pg').execute_query('x')", ["Sales:pg"]),
    ('a = ds_clients["A:c"]\nb = ds_clients.get("B:c", None)', ["A:c", "B:c"]),
    # generate_df's own parameter declares the dict; it does not reach it.
    ('def generate_df(ds_clients, excel_files):\n    return ds_clients["A:c"].execute_query("x")\n', ["A:c"]),
    # Python reads identifiers in NFKC form: a full-width underscore is the same name.
    ('ds＿clients["A:c"]', ["A:c"]),
    # generate_df receives the dict positionally, under whatever name it gives it.
    ('def generate_df(clients, excel_files):\n    return clients["A:c"].execute_query("x")\n', ["A:c"]),
    ('async def generate_df(c, excel_files):\n    return c.get("A:c")\n', ["A:c"]),
    # The run's namespace also binds the dict as the global `db_clients`.
    ('def generate_df(ds_clients, e):\n    ds_clients["Public:pg"]\n'
     '    return db_clients["Secret:pg"].execute_query("x")\n', ["Public:pg", "Secret:pg"]),
    # Comments, column names and other identifiers are not a reach.
    ('# ds_clients["A:c"] and ds_clients.values()\nsql = "SELECT amount FROM t"', []),
    # The libraries step code needs, and pandas query/eval without @names.
    ('import pandas as pd\nimport numpy as np\nfrom datetime import date\nimport json, re, math\n'
     'ds_clients["A:c"]\ndf.query("a > 1")', ["A:c"]),
    # A client's own .query(), the alias of execute_query, is not pandas'.
    ('ds_clients["Sales:pg"].query(sql)', ["Sales:pg"]),
    ('ds_clients["Sales:pg"].query(sql, params={"v": 1})', ["Sales:pg"]),
    ('ds_clients.get("Sales:pg").query("SELECT a FROM t WHERE x = \'1@b\'")', ["Sales:pg"]),
    ('db_clients["Sales:pg"].query(sql)', ["Sales:pg"]),
    ('def generate_df(c, e):\n    sales = c["Sales:pg"]\n    return sales.query(sql)\n', ["Sales:pg"]),
    # Machine learning and warnings, as the coder prompt teaches them.
    ('import sklearn\nfrom sklearn.linear_model import LinearRegression\nimport scipy.stats\n'
     'import warnings\nds_clients["A:c"]', ["A:c"]),
    ('from functools import reduce, partial\nfrom typing import Dict, List, Optional\nds_clients["A:c"]',
     ["A:c"]),
    # Public pandas / numpy submodules, as their attribute form already is.
    ('from pandas.api.types import is_numeric_dtype\nfrom pandas.tseries.offsets import MonthEnd\n'
     'from numpy.linalg import lstsq\nimport pandas.api.types as pt\nds_clients["A:c"]', ["A:c"]),
    # A `.core` attribute of something that is not pandas or numpy.
    ('rows = [r.core for r in df.itertuples()]\nds_clients["A:c"]', ["A:c"]),
    ("db_rows = 1", []),
    ("", []),
    (None, []),
])
def test_literal_keys_are_the_clients_the_code_reaches(code, expected):
    assert literal_client_keys(code) == expected


@pytest.mark.parametrize("code", [
    'key = "A:c"\nds_clients[key]',
    'c = ds_clients\nc["A:c"]',
    'ds_clients["A:c"]; list(ds_clients.values())',
    "for k, v in ds_clients.items():\n    pass",
    'ds_clients[f"{n}:c"]',
    "helper(ds_clients)",
    'if "A:c" in ds_clients:\n    pass',
    "ds_clients.get(name)",
    # Code that is not Python cannot be read, so it tells nothing either.
    "def generate_df(:\n",
    # A helper given the dict under a renamed parameter: the literal key inside
    # the helper does not tell which clients the renamed name reaches.
    'def fetch(ds_clients, sql):\n    return ds_clients["db:pg"].execute_query(sql)\n\n'
    'def generate_df(clients, excel_files):\n    fetch(clients, "x")\n'
    '    return clients["Warehouse:x::fast"].execute_query("y")\n',
    'def generate_df(clients, excel_files):\n    for c in clients.values():\n        pass\n',
    # The namespace reached by name.
    'locals()["ds_clients"]["A:c"]',
    'globals()["ds_clients"]',
    'vars()["ds_clients"]',
    'eval("ds_clients")',
    'exec("x = ds_clients")',
    # The run's namespace also binds the dict as the global `db_clients`.
    # generate_df that is not one plain module-level def taking the dict
    # first: what receives the dict is not known. (Each with a decoy key.)
    'ds_clients["A:c"]\ngenerate_df = lambda c, e: c["B:c"]\n',
    'ds_clients["A:c"]\ndef g(c, e):\n    return c["B:c"]\n\ngenerate_df = g\n',
    'import functools\nds_clients["A:c"]\ndef g(x, c, e):\n    return c["B:c"]\n\n'
    'generate_df = functools.partial(g, 1)\n',
    'ds_clients["A:c"]\n@wrap\ndef generate_df(c, e):\n    return c["B:c"]\n',
    'ds_clients["A:c"]\nclass generate_df:\n    def __init__(self, c, e):\n        self.r = c["B:c"]\n',
    'ds_clients["A:c"]\ndef generate_df(*a):\n    return a[0]["B:c"]\n',
    'def generate_df(c, e):\n    return c["A:c"]\n\ndef generate_df(d, e):\n    return d["B:c"]\n',
    'ds_clients["A:c"]\ndef outer():\n    def generate_df(c, e):\n        return c["B:c"]\n',
    'ds_clients["A:c"]\nfrom helpers import generate_df\n',
    # Other ways to reach a variable by name.
    'ds_clients["A:c"]\ngetattr(x, "y")',
    'ds_clients["A:c"]\n__builtins__["globals"]()',
    'ds_clients["A:c"]\nimport sys\nsys.modules["__main__"]',
    'ds_clients["A:c"]\nimport inspect\ninspect.currentframe()',
    'ds_clients["A:c"]\nfrom inspect import currentframe as cf\ncf()',
    'ds_clients["A:c"]\nf.f_globals["db_clients"]',
    'ds_clients["A:c"]\nf.f_locals',
    'def generate_df(ds_clients, e):\n    ds_clients["A:c"]\n    return generate_df.__globals__["db_clients"]\n',
    'ds_clients["A:c"]\n__import__("sys")',
    # Any other binding of the entry function's name, str-field binders included.
    'ds_clients["A:c"]\ndef generate_df(c, e):\n    return c["A:c"]\n'
    'match x:\n    case generate_df:\n        pass\n',
    'ds_clients["A:c"]\ndef generate_df(c, e):\n    return c["A:c"]\n'
    'match x:\n    case [*generate_df]:\n        pass\n',
    'ds_clients["A:c"]\ndef generate_df(c, e):\n    return c["A:c"]\n'
    'match x:\n    case {**generate_df}:\n        pass\n',
    'ds_clients["A:c"]\ndef generate_df(c, e):\n    return c["A:c"]\n'
    'try:\n    pass\nexcept Exception as generate_df:\n    pass\n',
    'ds_clients["A:c"]\ndef generate_df(c, e):\n    return c["A:c"]\nfrom helpers import *\n',
    'ds_clients["A:c"]\ndef generate_df(c, e):\n    return c["A:c"]\nx = "generate_df"\n',
    # A module off the data-library allowlist can reach objects by a string.
    'import operator\ndef generate_df(ds_clients, e):\n    ds_clients["A:c"]\n'
    '    return operator.attrgetter("__globals__")(generate_df)\n',
    'import gc\nds_clients["A:c"]\ngc.get_objects()',
    'import string\nds_clients["A:c"]\nstring.Formatter().get_field("0.__globals__", [f], {})',
    'from operator import attrgetter\nds_clients["A:c"]\nattrgetter("_rpc")(ds_clients["A:c"])',
    'def generate_df(ds_clients, e):\n    import types\n    ds_clients["A:c"]\n',
    # A private attribute of a client object.
    'ds_clients["A:c"]._rpc.call("execute_query", client="Secret:sf")',
    # pandas resolves @names (and the clients' names in strings) from the frame.
    'ds_clients["A:c"]\ndf.query("@db_clients")',
    'ds_clients["A:c"]\npd.eval(expr)',
    'ds_clients["A:c"]\nsql = "SELECT ds_clients FROM t"',
    # pandas' evaluators under another name, its internals, a bound method.
    'from pandas import eval as pe\nds_clients["A:c"]',
    'from pandas import query\nds_clients["A:c"]',
    'import pandas.core.frame\nds_clients["A:c"]',
    'from pandas.core import common\nds_clients["A:c"]',
    'import numpy.testing\nds_clients["A:c"]',
    'ds_clients["A:c"]\nx = pd.core.frame',
    'ds_clients["A:c"]\nx = np.testing',
    'ds_clients["A:c"]\nq = df.query\nq("a > 1")',
    'sales = ds_clients["A:c"]\nsales = df\nsales.query("x > @t")',
    # A name bound again by a parameter, an except clause, a match capture,
    # an import or a def is no client receiver.
    'db = ds_clients["A:pg"]\ndef f(db):\n    return db.query("x > @t")\n',
    'c = ds_clients["A:pg"]\ng = lambda c: c.query("x > @t")\n',
    'c = ds_clients["A:pg"]\ntry:\n    pass\nexcept Exception as c:\n    pass\nc.query("x > @t")\n',
    'c = ds_clients["A:pg"]\nmatch y:\n    case [*c]:\n        pass\nc.query("x > @t")\n',
    'c = ds_clients["A:pg"]\nmatch y:\n    case {**c}:\n        pass\nc.query("x > @t")\n',
    'c = ds_clients["A:pg"]\nfrom pandas import DataFrame as c\nc.query("x > @t")\n',
    'c = ds_clients["A:pg"]\ndef c():\n    pass\nc.query("x > @t")\n',
    # pandas / numpy internals by submodule.
    'import pandas._libs\nds_clients["A:c"]',
    'import pandas.core.computation\nds_clients["A:c"]',
    'from pandas import core\nds_clients["A:c"]',
    'from numpy import testing\nds_clients["A:c"]',
    'import numpy.ctypeslib\nds_clients["A:c"]',
    'import numpy.f2py\nds_clients["A:c"]',
    'from functools import wraps\nds_clients["A:c"]',
    'from typing import get_type_hints\nds_clients["A:c"]',
])
def test_any_other_reach_of_the_clients_means_unknown(code):
    assert literal_client_keys(code) is None
    assert client_reach(code).other is True


# The modules step code is expected to import: anything else counts as
# another reach (client_keys.DATA_LIBRARIES).
def _stdlib_modules_the_code_check_accepts():
    import sys as _sys

    from app.ai.code_execution.code_execution import FORBIDDEN_MODULES, ML_TRAINING_MODULES

    return sorted(m for m in _sys.stdlib_module_names
                  if not m.startswith("_") and m not in FORBIDDEN_MODULES | ML_TRAINING_MODULES)


def test_every_accepted_stdlib_module_off_the_allowlist_counts_as_another_reach():
    from app.services.client_keys import DATA_LIBRARIES

    off_list = [m for m in _stdlib_modules_the_code_check_accepts() if m not in DATA_LIBRARIES]
    assert off_list, "the code check accepts no stdlib module off the allowlist?"
    reached = {m: client_reach(f'import {m}\nds_clients["A:c"]').other for m in off_list}
    reached.update({f"from {m}": client_reach(f'from {m} import x\nds_clients["A:c"]').other
                    for m in off_list})
    assert all(reached.values()), sorted(k for k, v in reached.items() if not v)


def test_the_allowlisted_libraries_keep_the_literal_keys():
    from app.services.client_keys import DATA_LIBRARIES

    for m in sorted(DATA_LIBRARIES):
        assert literal_client_keys(f'import {m}\nds_clients["A:c"]') == ["A:c"], m


def test_the_literal_keys_are_kept_beside_another_reach():
    reach = client_reach('ds_clients["A:c"]\nfor c in ds_clients.values():\n    pass')
    assert reach.keys == ("A:c",)
    assert reach.other is True


@pytest.mark.parametrize("key, expected", [
    ("Sales:pg", ["Sales"]),
    ("Sales:pg::fast", ["Sales"]),
    ("Sales", ["Sales"]),  # the single-connection alias
    # The name must end at ':' -- "Sales Archive" is another source.
    ("Sales Archive:pg", ["Sales Archive"]),
    ("SalesX:pg", []),
    ("Sale:pg", []),
    # "Sales:EU:pg" is source "Sales:EU" on connection "pg", or source "Sales"
    # on a connection named "EU:pg": both count.
    ("Sales:EU:pg", ["Sales", "Sales:EU"]),
    ("Sales:EU", ["Sales", "Sales:EU"]),
    ("Other:pg", []),
])
def test_a_key_belongs_to_every_source_name_it_starts_with(key, expected):
    names = ["Sales", "Sales Archive", "Sales:EU", "Sale "]
    assert sorted(source_names_for_key(names, key)) == expected


def test_keys_map_to_source_ids_and_unmatched_keys_are_reported():
    rows = [("id-sales", "Sales"), ("id-eu", "Sales:EU"), ("id-db", "db")]
    picked, unmatched = sources_for_client_keys(
        rows, ["Sales:pg", "Sales:EU:pg::fast", "Sales:pg", "Gone:pg", "bow"])
    assert picked == ["id-sales", "id-eu"]
    # "bow" is the platform's own client, not an organization source.
    assert unmatched == ["Gone:pg"]


def test_a_source_named_like_the_platform_client_is_still_a_source():
    picked, unmatched = sources_for_client_keys([("id-bow", "bow")], ["bow"])
    assert (picked, unmatched) == (["id-bow"], [])


# Reports every global of the run's namespace (module-level dir(); the code
# check forbids globals()).
_NAMESPACE_PROBE = """
names_seen = sorted(dir())


def generate_df(ds_clients, excel_files):
    return pd.DataFrame({"name": names_seen})
"""
# Globals the run binds that are not the clients dict (helpers and data).
_NAMESPACE_HELPERS = {"pd", "np", "excel_files", "load_step", "load_entity", "read_text",
                      "http", "lists", "browser"}


@pytest.mark.parametrize("mode", ["subprocess", "inprocess"])
def test_every_namespace_global_is_a_known_helper_or_a_tracked_clients_name(monkeypatch, mode):
    """The run binds the clients dict under a global name besides handing it
    to generate_df. Every global it binds is either a known helper or a name
    the key rule tracks: a new global holding the dict cannot go unseen."""
    from app.ai.code_execution.code_execution import StreamingCodeExecutor
    from app.ai.code_execution.sandbox.namespace import CLIENTS_GLOBAL
    from app.services.client_keys import CLIENT_DICT_NAMES

    monkeypatch.setenv("BOW_CODE_SANDBOX", mode)
    df, _, _ = StreamingCodeExecutor(organization_settings=None).execute_code(
        code=_NAMESPACE_PROBE, ds_clients={"a:c": object()}, excel_files=[])
    bound = set(df["name"]) - {"__builtins__", "names_seen", "generate_df"}
    assert CLIENTS_GLOBAL in bound
    assert bound - _NAMESPACE_HELPERS <= CLIENT_DICT_NAMES, bound - _NAMESPACE_HELPERS - CLIENT_DICT_NAMES
