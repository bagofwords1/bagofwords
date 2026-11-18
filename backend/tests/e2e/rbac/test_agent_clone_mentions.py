"""Mentions when cloning an agent.

Contracts:
- Only an @ in user text that resolves to the source (longest name wins)
  moves to the clone; a name that merely starts with the source's doesn't.
- Stored mention objects of the source agent, its copied tables and its
  connection tools point at the clone; anything else (other agents, a table
  the clone doesn't have, saved queries, which are not cloned) stays put.
"""
import uuid

import pytest

from tests.e2e.rbac.test_agent_clone import _clone, _h, world  # noqa: F401  (fixture)

pytestmark = pytest.mark.e2e


def _entity(test_client, w, code, title):
    r = test_client.post("/api/entities", json={
        "type": "model", "title": title, "slug": f"q-{uuid.uuid4().hex[:8]}", "code": code, "data": {},
        "status": "published", "data_source_ids": [w["agent"]["id"]],
    }, headers=_h(w["admin"]["token"], w["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("source,clone,text,after", [
    ("Sales", "Sales copy", "ask @Sales now", 'ask @"Sales copy" now'),
    ("Sales", "Sales copy", 'ask @"Sales" now', 'ask @"Sales copy" now'),
    ("Sales", "Sales copy", "compare @SalesOps", "compare @SalesOps"),   # another agent whose name starts the same
    ("Sales", "Sales copy", "email sales@Salesforce.com", "email sales@Salesforce.com"),
    # Another agent's longer name wins, as when the text is used.
    ("Sales", "Sales copy", "see @Sales Orders", "see @Sales Orders"),
    ("מכירות", "מכירות 2", "שאל את @מכירות היום", 'שאל את @"מכירות 2" היום'),
    ("מכירות", "מכירות 2", "השווה עם @מכירות צפון", "השווה עם @מכירות צפון"),
    ("מכירות", "מכירות 2", "השווה עם @מכירותצפון", "השווה עם @מכירותצפון"),
])
def test_mentions_of_the_source_in_text_point_at_the_clone(source, clone, text, after):
    from types import SimpleNamespace

    from app.services.agent_clone_service import remap_mention_text
    from app.utils.mentions import MentionMatcher
    others = ["SalesOps", "Sales Orders", "מכירות צפון", "מכירותצפון"]
    ctx = SimpleNamespace(source=SimpleNamespace(name=source), clone=SimpleNamespace(name=clone),
                          mention_matcher=MentionMatcher([source, *others]))
    assert remap_mention_text(text, ctx) == after


def test_a_mention_of_a_table_the_clone_doesnt_have_stays_on_the_source():
    from types import SimpleNamespace

    from app.services.agent_clone_service import remap_mentions
    ctx = SimpleNamespace(source_id="src", clone_id="dst", clone=SimpleNamespace(name="Sales copy"),
                          table_map={"t1": "t1-copy"})
    copied = {"type": "datasource_table", "id": "t1", "data_source_id": "src", "data_source_name": "Sales"}
    users = {"type": "datasource_table", "id": "t-user", "data_source_id": "src", "data_source_name": "Sales"}
    assert remap_mentions(copied, ctx) == {**copied, "id": "t1-copy", "data_source_id": "dst",
                                           "data_source_name": "Sales copy"}
    assert remap_mentions(users, ctx) == users


def test_mentions_are_re_pointed_in_both_stored_shapes():
    """Some surfaces keep typed items (data_source_id) inside the groups; the
    prompt editor's groups hold untyped items (datasource_id)."""
    from types import SimpleNamespace

    from app.services.agent_clone_service import remap_mentions
    ctx = SimpleNamespace(source_id="src", clone_id="dst", clone=SimpleNamespace(name="Sales copy"),
                          table_map={"t1": "t1-copy"})
    typed = [{"name": "DATA SOURCES", "items": [{"type": "data_source", "id": "src", "name": "Sales"}]},
             {"name": "TABLES", "items": [{"type": "datasource_table", "id": "t1", "data_source_id": "src",
                                           "data_source_name": "Sales"}]}]
    grouped = [{"name": "DATA SOURCES", "items": [{"id": "src", "name": "Sales"}, {"id": "other", "name": "Legal"}]},
               {"name": "TABLES", "items": [{"id": "t1", "name": "orders", "datasource_id": "src",
                                             "data_source_name": "Sales"}]},
               {"name": "ENTITIES", "items": [{"id": "e1", "title": "Top rows", "entity_type": "query"}]},
               {"name": "FILES", "items": [{"id": "src", "filename": "src.csv"}]}]
    assert remap_mentions(typed, ctx) == [
        {"name": "DATA SOURCES", "items": [{"type": "data_source", "id": "dst", "name": "Sales copy"}]},
        {"name": "TABLES", "items": [{"type": "datasource_table", "id": "t1-copy", "data_source_id": "dst",
                                      "data_source_name": "Sales copy"}]}]
    assert remap_mentions(grouped, ctx) == [
        {"name": "DATA SOURCES", "items": [{"id": "dst", "name": "Sales copy"}, {"id": "other", "name": "Legal"}]},
        {"name": "TABLES", "items": [{"id": "t1-copy", "name": "orders", "datasource_id": "dst",
                                      "data_source_name": "Sales copy"}]},
        # Saved queries are not cloned: a mention of one stays on the source's.
        {"name": "ENTITIES", "items": [{"id": "e1", "title": "Top rows", "entity_type": "query"}]},
        # A file whose id happens to match is not an agent.
        {"name": "FILES", "items": [{"id": "src", "filename": "src.csv"}]}]


def test_group_names_match_in_any_case_and_connection_tools_follow():
    """mention_service reads group names upper-cased; CONNECTION TOOLS items
    carry the agent as data_source_id (frontend/utils/mentions.ts)."""
    from types import SimpleNamespace

    from app.services.agent_clone_service import remap_mentions
    ctx = SimpleNamespace(source_id="src", clone_id="dst", clone=SimpleNamespace(name="Sales copy"),
                          table_map={"t1": "t1-copy"})
    out = remap_mentions([
        {"name": "Data Sources", "items": [{"id": "src", "name": "Sales"}]},
        {"name": "tables", "items": [{"id": "t1", "name": "orders", "datasource_id": "src", "data_source_name": "Sales"}]},
        {"name": "CONNECTION TOOLS", "items": [{"id": "tool-1", "name": "get_orders", "data_source_id": "src"}]},
    ], ctx)
    assert out[0]["items"] == [{"id": "dst", "name": "Sales copy"}]
    assert out[1]["items"][0]["id"] == "t1-copy" and out[1]["items"][0]["datasource_id"] == "dst"
    assert out[2]["items"] == [{"id": "tool-1", "name": "get_orders", "data_source_id": "dst"}]


@pytest.mark.parametrize("mentions,rewritten", [
    ([{"name": "TABLES", "items": [{"id": "t1", "name": "orders", "datasource_id": "src"}]}], False),
    ([{"name": "DATA SOURCES", "items": [{"id": "src", "name": "orders"}]}], True),
    ([{"name": "TABLES", "items": [{"type": "datasource_table", "id": "t1", "name": "orders", "data_source_id": "src"}]},
      {"name": "DATA SOURCES", "items": [{"type": "data_source", "id": "src", "name": "orders"}]}], True),
    (None, True),
])
def test_an_agent_named_like_its_table_is_told_apart_by_the_stored_mentions(mentions, rewritten):
    from types import SimpleNamespace

    from app.services.agent_clone_service import remap_mention_text
    from app.utils.mentions import MentionMatcher
    ctx = SimpleNamespace(source=SimpleNamespace(name="orders"), clone=SimpleNamespace(name="orders-copy"),
                          source_id="src", mention_matcher=MentionMatcher(["orders"]), source_name_is_a_table=True)
    out = remap_mention_text("@orders by month", ctx, mentions)
    assert out == ('@"orders-copy" by month' if rewritten else "@orders by month")


def test_a_saved_query_named_after_the_source_is_not_taken_for_it(test_client, world):
    """The dictionary holds the source's saved queries: @<source> Pipeline is
    the query "<source> Pipeline", not the agent followed by a word."""
    h = _h(world["admin"]["token"], world["org_id"])
    name = world["agent"]["name"]
    _entity(test_client, world, "df = None", f"{name} Pipeline")
    r = test_client.post("/api/prompts", json={
        "title": "Pipeline", "text": f"@{name} Pipeline last week", "scope": "agent",
        "data_source_ids": [world["agent"]["id"]],
    }, headers=h)
    assert r.status_code == 200, r.text
    clone = _clone(test_client, world).json()["data_source"]
    res = test_client.get("/api/prompts", params={"data_source_id": clone["id"]}, headers=h)
    [copy] = [p for p in res.json()["prompts"] if p["title"] == "Pipeline"]
    assert copy["text"] == f"@{name} Pipeline last week"
