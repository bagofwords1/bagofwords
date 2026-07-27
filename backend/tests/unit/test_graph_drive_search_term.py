"""Fix validation: Graph drive search must survive wildcard/punctuation queries.

`search_files` interpolates the query into the URL PATH
(`/drives/{id}/root/search(q='…')`). ASP.NET rejects the whole request with
`400 invalidRequest "A potentially dangerous Request.Path value was detected"`
when the term contains `*`, `?`, `%`, `&`, `#`, … — even percent-encoded.

Observed live: a model asked to find an image searched for `*.png`, and the call
400'd on BOTH OneDrive and SharePoint, so the agent concluded the file did not
exist (it did). Graph drive search is a substring match with no wildcard
support, so stripping wildcards preserves the caller's intent.

Run:
    cd backend && python -m pytest tests/unit/test_graph_drive_search_term.py -v
"""
import pytest

from app.data_sources.clients.graph_drive_client import GraphDriveClient

term = GraphDriveClient._search_term


@pytest.mark.parametrize("raw,expected", [
    ("*.png", ".png"),          # the exact query that broke live
    ("*.xlsx", ".xlsx"),
    ("report?.csv", "report .csv".replace(" ", "%20")),
    ("2017_Expense_Data.csv", "2017_Expense_Data.csv"),   # ordinary names untouched
])
def test_wildcards_are_stripped_but_intent_kept(raw, expected):
    assert term(raw) == expected


@pytest.mark.parametrize("raw", ["a/b", "reports/2024.xlsx", "../secrets", "/etc/passwd"])
def test_slashes_never_reach_the_url_path(raw):
    """`quote` keeps '/' by default, so a slash (or '../') would survive into
    `/drives/{id}/root/search(q='…')` and change which endpoint is addressed
    rather than what is searched for."""
    out = term(raw)
    assert "/" not in out
    assert "%2F" not in out.upper(), "the slash is dropped, not encoded — it is not a search term"


@pytest.mark.parametrize("ch", list("*?%&#<>:\\\"|/"))
def test_no_path_hostile_character_survives(ch):
    # Compare on the DECODED term: percent-encoding is the safe representation,
    # so `%20` legitimately contains '%'. What must not survive is a literal
    # hostile character in the term Graph ends up parsing.
    from urllib.parse import unquote

    out = unquote(term(f"a{ch}b"))
    assert ch not in out, f"{ch!r} must not reach the URL path"


def test_all_wildcard_query_falls_back_to_a_usable_term():
    # "*" / "*.*" would collapse to an empty term, which Graph also rejects.
    for raw in ("*", "*.*", "   "):
        assert term(raw) == ".", raw


def test_spaces_are_collapsed_and_encoded():
    assert term("  sales   report  ") == "sales%20report"


def test_none_is_safe():
    assert term(None) == "."
