"""Old client keys after a rename (app/services/client_keys.py,
app/models/former_names.py).

A renamed agent or connection keeps answering to the keys saved code spelled
with its former name -- but never to a key another live agent could claim.
"""
import pytest

from app.services.client_keys import (
    FORMER_KEYS_ATTR,
    former_client_keys,
    sources_for_client_keys,
    with_former_keys,
)


class _Client:
    def __init__(self, former=()):
        if former:
            setattr(self, FORMER_KEYS_ATTR, tuple(former))


def test_a_renamed_agent_answers_to_its_old_keys():
    old = former_client_keys("Lake", ["DWH"], [("pg", [], False, "c1")], [], bare_owners={"DWH": "c1"})
    # The pair key and the bare-name alias of the connection that had it.
    assert old == {"DWH:pg": "Lake:pg", "DWH": "Lake:pg"}


def test_a_renamed_connection_answers_to_its_old_key():
    old = former_client_keys("Lake", [], [("pg2", ["pg"], False, "id-pg2"), ("bq", [], False, "id-bq")], [])
    assert old == {"Lake:pg": "Lake:pg2"}


def test_every_combination_of_former_names_is_an_old_key():
    old = former_client_keys("C", ["A", "B"], [("z", ["x", "y"], False, "id-z")], [])
    pairs = {f"{s}:{c}" for s in "ABC" for c in "xyz"} - {"C:z"}
    assert {k for k in old if ":" in k} == pairs
    assert set(old.values()) == {"C:z"}


def test_the_bare_alias_is_not_answered_when_the_agent_had_several_connections_then():
    # No connection owned the bare key when the name was given up.
    old = former_client_keys("Lake", ["DWH"], [("pg", [], False, "c1"), ("bq", [], False, "c2")], [])
    assert "DWH" not in old
    assert old == {"DWH:pg": "Lake:pg", "DWH:bq": "Lake:bq"}


def test_the_bare_alias_belongs_to_the_connection_that_had_it():
    # The agent gained a second connection since: the bare old key is still
    # the first one's.
    old = former_client_keys("Lake", ["DWH"], [("pg", [], False, "c1"), ("bq", [], False, "c2")], [],
                             bare_owners={"DWH": "c1"})
    assert old["DWH"] == "Lake:pg"


@pytest.mark.parametrize("connections", [
    [("other", [], False, "c9")],                       # the connection was replaced
    [],                                                 # ... or went down / was detached
])
def test_the_bare_alias_is_not_answered_by_a_replacement_connection(connections):
    old = former_client_keys("Lake", ["DWH"], connections, [], bare_owners={"DWH": "c1"})
    assert "DWH" not in old


def test_the_fast_sibling_follows_its_connection_but_not_the_bare_alias():
    old = former_client_keys("Lake", ["DWH"], [("pg", [], True, "c1")], [], bare_owners={"DWH": "c1"})
    assert old["DWH:pg::fast"] == "Lake:pg::fast"
    assert old["DWH"] == "Lake:pg"
    assert "DWH::fast" not in old


@pytest.mark.parametrize("other", [
    ("DWH", []),          # another agent carries the old name now
    ("Mart", ["DWH"]),    # another agent used to carry it too
])
def test_an_old_key_another_agent_could_claim_is_not_answered(other):
    assert former_client_keys("Lake", ["DWH"], [("pg", [], False, "c1")], [other],
                              bare_owners={"DWH": "c1"}) == {}


def test_an_unrelated_agent_does_not_block_old_keys():
    old = former_client_keys("Lake", ["DWH"], [("pg", [], False, "id-pg")], [("DWH Archive", ["DW"])])
    assert "DWH:pg" in old


def test_a_current_key_is_never_an_old_key():
    # Connection "pg" was renamed to "pg2", then another connection took "pg".
    old = former_client_keys("Lake", [], [("pg2", ["pg"], False, "id-pg2"), ("pg", [], False, "id-pg")], [])
    assert old == {}


def test_an_old_key_two_connections_could_answer_is_not_answered():
    old = former_client_keys("Lake", [], [("a", ["x"], False, "id-a"), ("b", ["x"], False, "id-b")], [])
    assert "Lake:x" not in old


def test_an_old_key_answers_a_lookup_without_replacing_a_current_key():
    renamed, current = _Client(former=["DWH:pg", "Other:pg"]), _Client()
    clients = {"Lake:pg": renamed, "Other:pg": current}
    merged = with_former_keys(clients)
    assert merged["DWH:pg"] is renamed and merged.get("DWH:pg") is renamed and "DWH:pg" in merged
    assert merged["Other:pg"] is current
    # The dict the prompt lists is left alone.
    assert set(clients) == {"Lake:pg", "Other:pg"}


def test_old_keys_are_not_part_of_what_code_iterates():
    """Code that walks every client must reach each one once: an old key is
    a way to ask for a client, not another client."""
    a, b = _Client(former=["DWH:pg", "DWH:old"]), _Client()
    merged = with_former_keys({"Lake:pg": a, "Lake:bq": b})
    assert len(merged) == 2
    assert list(merged) == list(merged.keys()) == ["Lake:pg", "Lake:bq"]
    assert list(merged.values()) == [a, b]
    assert [k for k, _ in merged.items()] == ["Lake:pg", "Lake:bq"]


def test_a_key_never_held_is_still_missing():
    merged = with_former_keys({"Lake:pg": _Client(former=["DWH:pg"])})
    assert merged.get("Gone:pg") is None and "Gone:pg" not in merged
    with pytest.raises(KeyError):
        merged["Gone:pg"]


def test_with_former_keys_accepts_no_clients():
    assert len(with_former_keys(None)) == 0


class _CountingClient:
    """A queryable client that answers 1 row and counts its queries."""

    def __init__(self, former=()):
        self.queries = 0
        if former:
            setattr(self, FORMER_KEYS_ATTR, tuple(former))

    def execute_query(self, sql):
        import pandas as pd

        self.queries += 1
        return pd.DataFrame({"n": [1]})


_WALK_EVERY_CLIENT = """
def generate_df(ds_clients, excel_files):
    total = sum(int(c.execute_query('select 1')['n'].iloc[0]) for c in ds_clients.values())
    by_old_key = int(ds_clients[OLD_KEY].execute_query('select 1')['n'].iloc[0])
    return pd.DataFrame({"clients": [len(ds_clients)], "total": [total], "by_old_key": [by_old_key]})
"""


@pytest.mark.parametrize("mode", ["subprocess", "inprocess"])
def test_a_run_reaches_each_client_once_and_still_resolves_the_old_key(monkeypatch, mode):
    """Two connections, one renamed: saved code that sums over every client
    must not count the renamed one twice, in either executor mode."""
    from app.ai.code_execution.code_execution import StreamingCodeExecutor

    monkeypatch.setenv("BOW_CODE_SANDBOX", mode)
    renamed, other = _CountingClient(former=["Lake:pg"]), _CountingClient()
    code = _WALK_EVERY_CLIENT.replace("OLD_KEY", "'Lake:pg'")
    df, _, _ = StreamingCodeExecutor(organization_settings=None).execute_code(
        code=code, ds_clients={"Lake:warehouse": renamed, "Lake:bq": other}, excel_files=[])
    row = df.iloc[0]
    assert (int(row["clients"]), int(row["total"]), int(row["by_old_key"])) == (2, 2, 1)
    assert (renamed.queries, other.queries) == (2, 1)


# --- the "which sources does this code use" rule follows the rename ---------

def test_a_key_saved_before_a_rename_names_the_renamed_source():
    rows = [("id-lake", "Lake", ["DWH"]), ("id-db", "db", None)]
    assert sources_for_client_keys(rows, ["DWH:pg", "DWH"]) == (["id-lake"], [])


def test_a_current_name_wins_over_a_former_one():
    rows = [("id-lake", "Lake", ["DWH"]), ("id-new", "DWH", None)]
    assert sources_for_client_keys(rows, ["DWH:pg"]) == (["id-new"], [])


def test_a_former_name_two_sources_had_counts_both():
    rows = [("id-a", "Lake", ["DWH"]), ("id-b", "Mart", ["DWH"])]
    picked, unmatched = sources_for_client_keys(rows, ["DWH:pg"])
    assert sorted(picked) == ["id-a", "id-b"] and unmatched == []


def test_a_former_name_counts_only_as_a_client_key_not_as_a_word_in_the_code():
    """An agent once called "orders" is not used by every query that reads
    FROM orders (the dashboard chat picks its agents by this rule)."""
    from app.services.client_keys import client_reach

    rows = [("id-shop", "Shop", None), ("id-renamed", "Fulfilment", [{"name": "orders", "connection_id": None}])]
    code = ("def generate_df(ds_clients, excel_files):\n"
            "    return ds_clients['Shop:pg'].execute_query('SELECT * FROM orders')\n")
    assert sources_for_client_keys(rows, client_reach(code).keys) == (["id-shop"], [])


def test_former_names_are_read_in_either_stored_shape():
    rows = [("id-a", "Lake", [{"name": "DWH", "connection_id": "c1"}, "Mart"])]
    assert sources_for_client_keys(rows, ["DWH:pg", "Mart:pg"]) == (["id-a"], [])


def test_rows_without_former_names_still_work():
    assert sources_for_client_keys([("id-a", "Lake")], ["Lake:pg", "Gone:pg"]) == (["id-a"], ["Gone:pg"])
