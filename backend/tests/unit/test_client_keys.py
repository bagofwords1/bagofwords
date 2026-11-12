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
    # Comments, strings, column names and other identifiers are not a reach.
    ('# ds_clients["A:c"] and ds_clients.values()\nsql = "SELECT ds_clients FROM t"', []),
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
])
def test_any_other_reach_of_the_clients_means_unknown(code):
    assert literal_client_keys(code) is None
    assert client_reach(code).other is True


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
