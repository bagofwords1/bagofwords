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
    old = former_client_keys("Lake", ["DWH"], [("pg", [], False)], [])
    # The pair key and, on a single-connection agent, the bare-name alias.
    assert old == {"DWH:pg": "Lake:pg", "DWH": "Lake:pg"}


def test_a_renamed_connection_answers_to_its_old_key():
    old = former_client_keys("Lake", [], [("pg2", ["pg"], False), ("bq", [], False)], [])
    assert old == {"Lake:pg": "Lake:pg2"}


def test_every_combination_of_former_names_is_an_old_key():
    old = former_client_keys("C", ["A", "B"], [("z", ["x", "y"], False)], [])
    pairs = {f"{s}:{c}" for s in "ABC" for c in "xyz"} - {"C:z"}
    assert {k for k in old if ":" in k} == pairs
    assert set(old.values()) == {"C:z"}


def test_the_bare_alias_needs_a_single_connection():
    old = former_client_keys("Lake", ["DWH"], [("pg", [], False), ("bq", [], False)], [])
    assert "DWH" not in old
    assert old == {"DWH:pg": "Lake:pg", "DWH:bq": "Lake:bq"}


def test_the_fast_sibling_follows_its_connection_but_not_the_bare_alias():
    old = former_client_keys("Lake", ["DWH"], [("pg", [], True)], [])
    assert old["DWH:pg::fast"] == "Lake:pg::fast"
    assert "DWH::fast" not in old


@pytest.mark.parametrize("other", [
    ("DWH", []),          # another agent carries the old name now
    ("Mart", ["DWH"]),    # another agent used to carry it too
])
def test_an_old_key_another_agent_could_claim_is_not_answered(other):
    assert former_client_keys("Lake", ["DWH"], [("pg", [], False)], [other]) == {}


def test_an_unrelated_agent_does_not_block_old_keys():
    old = former_client_keys("Lake", ["DWH"], [("pg", [], False)], [("DWH Archive", ["DW"])])
    assert "DWH:pg" in old


def test_a_current_key_is_never_an_old_key():
    # Connection "pg" was renamed to "pg2", then another connection took "pg".
    old = former_client_keys("Lake", [], [("pg2", ["pg"], False), ("pg", [], False)], [])
    assert old == {}


def test_an_old_key_two_connections_could_answer_is_not_answered():
    old = former_client_keys("Lake", [], [("a", ["x"], False), ("b", ["x"], False)], [])
    assert "Lake:x" not in old


def test_old_keys_are_added_for_the_run_without_replacing_a_current_key():
    renamed, current = _Client(former=["DWH:pg", "Other:pg"]), _Client()
    clients = {"Lake:pg": renamed, "Other:pg": current}
    merged = with_former_keys(clients)
    assert merged["DWH:pg"] is renamed
    assert merged["Other:pg"] is current
    # The dict the prompt lists is left alone.
    assert set(clients) == {"Lake:pg", "Other:pg"}


def test_with_former_keys_accepts_no_clients():
    assert with_former_keys(None) == {}


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


def test_rows_without_former_names_still_work():
    assert sources_for_client_keys([("id-a", "Lake")], ["Lake:pg", "Gone:pg"]) == (["id-a"], ["Gone:pg"])
