"""Unit tests for search_mcps query matching (filter_tools_by_query).

The helper is intentionally dependency-free, so we load it directly by file
path. This keeps the test fast and runnable without importing the full app
(the implementations package auto-imports every tool on import).
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

_HELPER = (
    Path(__file__).resolve().parents[2]
    / "app" / "ai" / "tools" / "implementations" / "_search_mcps_query.py"
)
_spec = importlib.util.spec_from_file_location("_search_mcps_query", _HELPER)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
filter_tools_by_query = _mod.filter_tools_by_query


def _tool(name, description=""):
    return SimpleNamespace(name=name, description=description)


def _names(tools):
    return [t.name for t in tools]


# A small Intercom-like tool set mirroring the PR's examples.
TOOLS = [
    _tool("search", "Unified search across Intercom"),
    _tool("search_articles", "Search help center articles"),
    _tool("search_contacts", "Search contacts by attributes"),
    _tool("search_conversations", "Search conversations"),
    _tool("get_company", "Retrieve a company by id"),
    _tool("list_companies", "List all companies"),
    _tool("get_contact", "Retrieve a single contact"),
]


def test_empty_query_returns_all():
    assert _names(filter_tools_by_query(TOOLS, None)) == _names(TOOLS)
    assert _names(filter_tools_by_query(TOOLS, "   ")) == _names(TOOLS)


def test_exact_name_token_matches():
    out = filter_tools_by_query(TOOLS, "search_contacts")
    # search_contacts scores highest (both tokens), others with 'search' follow.
    assert out[0].name == "search_contacts"
    assert "search_contacts" in _names(out)


def test_natural_language_query_ranks_relevant_first():
    out = filter_tools_by_query(TOOLS, "contacts for company")
    names = _names(out)
    # Must surface contact + company tools, not an empty list.
    assert "search_contacts" in names
    assert "get_company" in names
    assert len(names) > 0


def test_unmatched_query_falls_back_to_all():
    # A UUID-shaped / irrelevant query matched nothing under the old substring
    # filter -> 0 tools. Now it must fall back to ALL tools.
    out = filter_tools_by_query(TOOLS, "2e7eabb8-1311-4a1f-9c2d-000000000000")
    assert _names(out) == _names(TOOLS)

    out2 = filter_tools_by_query(TOOLS, "users")
    assert _names(out2) == _names(TOOLS)


def test_wildcard_prefix_matches_name():
    out = filter_tools_by_query(TOOLS, "search_*")
    names = _names(out)
    assert set(names) == {"search_articles", "search_contacts", "search_conversations"}


def test_wildcard_contains_matches_name_and_description():
    out = filter_tools_by_query(TOOLS, "*contact*")
    names = _names(out)
    # Matches names (search_contacts, get_contact) and descriptions mentioning contact.
    assert "search_contacts" in names
    assert "get_contact" in names


def test_wildcard_no_match_falls_back_to_all():
    out = filter_tools_by_query(TOOLS, "*nonexistent*")
    assert _names(out) == _names(TOOLS)


def test_question_mark_wildcard():
    tools = [_tool("getX"), _tool("getXY"), _tool("set")]
    out = filter_tools_by_query(tools, "get?")
    # '?' matches exactly one char -> 'getX' only.
    assert _names(out) == ["getX"]


def test_short_tokens_ignored_but_not_empty():
    # 'id' is < 3 chars -> no usable tokens -> return all rather than empty.
    out = filter_tools_by_query(TOOLS, "id")
    assert _names(out) == _names(TOOLS)
