"""The agents of a dashboard's card, and the agent filter (dashboards list
findability, P6).

A card of `GET /api/artifacts` lists the dashboard's agents (`agents`: id,
name, type and the same `icon_token` the agents page shows):

- a published dashboard's (its release is live) are its release's data
  sources (what readers' runs use);
- another's are the data sources of the conversation that wrote its draft's
  newest version (what a publish would copy), whoever's that is.

Agents are listed by name ignoring case, then id. `agent_ids`
(comma-separated) keeps the cards with any of those agents, by the same
rule. Only agents the reader can view (`visible_data_source_ids`: public
ones, ones they hold a grant on, every one for an org admin) that the
agents page also lists for them (not disabled; a draft only for its
managers; a `development` one only for agent admins) are shown or filtered
by: any other agent is never named to them, and filtering by it alone lists
nothing.

Associations and agents no route writes on its own (a conversation's agents
changed after the publish, a release's agent, an agent without a connection
or with two) are written directly; each place says so.
"""
import uuid
from datetime import datetime

import pytest

from tests.e2e.dashboards.dashboard_setup import as_, own, publish
from tests.e2e.dashboards.test_collaboration import (  # noqa: F401  (pytest fixtures)
    _edit,
    _finish,
    _grant_editor,
    drawn,
    new_version,
    spawned,
)

CONV = "conv_three_dash"
SALES, STOCK = f"{CONV}/dash_sales", f"{CONV}/dash_stock"
MULTI = "multi_source/dash"  # the conversation has chinook and "מחסן"
WAREHOUSE = "מחסן"  # the corpus's public agent over a postgresql connection


# ── helpers ───────────────────────────────────────────────────────────────

def _list(test_client, corpus, person, **params) -> dict:
    params.setdefault("limit", 100)
    resp = test_client.get("/api/artifacts", params=params, headers=as_(corpus, person))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Every candidate is on the page and admitted: the count is exact here.
    assert body["meta"]["total"] == len(body["dashboards"]), body["meta"]
    return body


def _cards(test_client, corpus, person, **params) -> dict:
    return {d["id"]: d for d in _list(test_client, corpus, person, **params)["dashboards"]}


def _agents(card) -> set[str]:
    return {a["id"] for a in card["agents"]}


def _sharing(test_client, corpus, aid, visibility):
    resp = test_client.put(f"/api/artifacts/{aid}/sharing", headers=as_(corpus, "owner"),
                           json={"visibility": visibility, "user_ids": [], "group_ids": []})
    assert resp.status_code == 200, resp.text


def _attach_to_conversation(corpus_rows, report_id, data_source_id):
    """A direct write: a conversation's agents change only inside an agent
    run; this is the state after one added an agent."""
    corpus_rows.insert("report_data_source_association", report_id=report_id, data_source_id=data_source_id)


def _attach_to_release(corpus_rows, release_id, data_source_id):
    """A direct write: a release takes its conversation's agents when it is
    published; this adds one without publishing again."""
    corpus_rows.insert("artifact_release_data_source_association", artifact_release_id=release_id,
                       data_source_id=data_source_id)


def _set_agent(test_client, corpus, data_source_id, **fields):
    resp = test_client.put(f"/api/data_sources/{data_source_id}", json=fields, headers=as_(corpus, "admin"))
    assert resp.status_code == 200, resp.text


def _bare_agent(corpus_rows, corpus, name, *connections) -> str:
    """A public agent over these connections, (type, created_at) each, linked
    in the order given. Direct writes: no route makes an agent without a
    connection, or dates its connections."""
    org = corpus.cast.org_id
    dsid = corpus_rows.insert("data_sources", name=name, organization_id=org, is_public=True)
    for conn_type, created_at in connections:
        cid = corpus_rows.insert("connections", name=f"{name} {conn_type}", type=conn_type,
                                 config={"host": "localhost", "port": 5432, "database": "nope"},
                                 organization_id=org, auth_policy="system_only", created_at=created_at)
        corpus_rows.insert("domain_connection", data_source_id=dsid, connection_id=cid)
    return dsid


# ── which agents a card lists ─────────────────────────────────────────────

@pytest.mark.e2e
def test_published_cards_show_the_release_agents_and_drafts_the_conversations(corpus, corpus_rows, test_client):
    chinook, warehouse = corpus.cast.data_sources["chinook"], corpus.cast.data_sources[WAREHOUSE]
    sales, stock, multi = own(corpus_rows, corpus, SALES), own(corpus_rows, corpus, STOCK), own(corpus_rows, corpus, MULTI)

    mine = _cards(test_client, corpus, "owner", filter="my")
    assert _agents(mine[sales]) == {chinook}
    assert _agents(mine[multi]) == {chinook, warehouse}

    publish(sales)  # the release takes the conversation's agents: chinook
    _sharing(test_client, corpus, sales, "internal")
    # The conversation goes on with another agent after the publish.
    _attach_to_conversation(corpus_rows, corpus.reports[CONV], warehouse)

    mine = _cards(test_client, corpus, "owner", filter="my")
    assert mine[sales]["published"] is True and _agents(mine[sales]) == {chinook}  # the release's
    assert mine[stock]["published"] is False and _agents(mine[stock]) == {chinook, warehouse}  # the conversation's
    reader = _cards(test_client, corpus, "member", filter="shared")[sales]
    assert _agents(reader) == {chinook}
    agent = next(a for a in reader["agents"] if a["id"] == chinook)
    assert agent["name"] and agent["type"] == "sqlite"


@pytest.mark.e2e
def test_agent_filter(corpus, corpus_rows, test_client):
    chinook, warehouse = corpus.cast.data_sources["chinook"], corpus.cast.data_sources[WAREHOUSE]
    sales, stock, multi = own(corpus_rows, corpus, SALES), own(corpus_rows, corpus, STOCK), own(corpus_rows, corpus, MULTI)
    publish(sales)
    _sharing(test_client, corpus, sales, "internal")
    _attach_to_conversation(corpus_rows, corpus.reports[CONV], warehouse)

    def ids(person, **params):
        return set(_cards(test_client, corpus, person, **params))

    everything = _cards(test_client, corpus, "owner", filter="my")
    for agent in (chinook, warehouse):
        # The filter keeps exactly the cards that list the agent.
        assert ids("owner", filter="my", agent_ids=agent) == {
            aid for aid, card in everything.items() if agent in _agents(card)}
    on_warehouse = ids("owner", filter="my", agent_ids=warehouse)
    assert {stock, multi} <= on_warehouse and sales not in on_warehouse  # published: the release's agents
    assert {sales, stock, multi} <= ids("owner", filter="my", agent_ids=chinook)
    # Several ids: any of them; blanks and repeats ignored.
    assert ids("owner", filter="my", agent_ids=f" {warehouse}, ,{chinook},{warehouse}") == {
        aid for aid, card in everything.items() if _agents(card) & {chinook, warehouse}}
    assert ids("owner", filter="my", agent_ids=str(uuid.uuid4())) == set()
    # On Shared too, with the reader's agents.
    assert sales in ids("member", filter="shared", agent_ids=chinook)
    assert sales not in ids("member", filter="shared", agent_ids=warehouse)


@pytest.mark.e2e
def test_agent_ids_are_validated_like_owner_ids(corpus, test_client):
    def status(value):
        return test_client.get("/api/artifacts", params={"agent_ids": value},
                               headers=as_(corpus, "owner")).status_code

    assert status(",".join(str(uuid.uuid4()) for _ in range(101))) == 422
    assert status(f"{uuid.uuid4()},not-an-id") == 422
    assert status("x" * 5000) == 422
    assert status(",".join(str(uuid.uuid4()) for _ in range(100))) == 200
    assert status(",, ,") == 200


@pytest.mark.e2e
def test_an_agent_the_reader_cannot_view_is_neither_shown_nor_filterable(corpus, corpus_rows, test_client,
                                                                         sqlite_data_source):
    chinook = corpus.cast.data_sources["chinook"]
    private = sqlite_data_source(name="Private agent", user_token=corpus.cast.people["admin"].token,
                                 org_id=corpus.cast.org_id, is_public=False)["id"]
    sales, stock = own(corpus_rows, corpus, SALES), own(corpus_rows, corpus, STOCK)
    release = publish(sales)
    _sharing(test_client, corpus, sales, "internal")
    _attach_to_release(corpus_rows, release, private)
    _attach_to_conversation(corpus_rows, corpus.reports[CONV], private)

    # A member cannot view it: not on the card, and filtering by it lists nothing.
    member = _cards(test_client, corpus, "member", filter="shared")
    assert _agents(member[sales]) == {chinook}
    assert _list(test_client, corpus, "member", filter="shared", agent_ids=private)["dashboards"] == []
    assert sales in _cards(test_client, corpus, "member", filter="shared", agent_ids=f"{private},{chinook}")
    # Nor can the owner (no grant on it): their draft's card leaves it out too.
    mine = _cards(test_client, corpus, "owner", filter="my")
    assert _agents(mine[stock]) == {chinook} and _agents(mine[sales]) == {chinook}
    assert _list(test_client, corpus, "owner", filter="my", agent_ids=private)["dashboards"] == []
    # The org admin can view every agent: it is on the card and filterable.
    admin = _cards(test_client, corpus, "admin", filter="shared")
    assert _agents(admin[sales]) == {chinook, private}
    assert sales in _cards(test_client, corpus, "admin", filter="shared", agent_ids=private)


@pytest.mark.e2e
def test_the_chinook_fixture_is_present():
    """The private-agent test (and the whole corpus) uses sqlite_data_source,
    which skips without the Chinook file; here its absence fails instead."""
    from tests.e2e.rbac.conftest import CHINOOK_PATH

    assert CHINOOK_PATH.exists(), f"missing {CHINOOK_PATH}: the agent tests would be skipped"


@pytest.mark.e2e
def test_icon_token_matches_the_agents_route(corpus, corpus_rows, test_client):
    chinook, warehouse = corpus.cast.data_sources["chinook"], corpus.cast.data_sources[WAREHOUSE]
    multi = own(corpus_rows, corpus, MULTI)
    # One agent with the admin's icon override, one with the icon its connection gives.
    resp = test_client.put(f"/api/data_sources/{chinook}", json={"icon": "emoji:🎵"}, headers=as_(corpus, "admin"))
    assert resp.status_code == 200, resp.text

    route = test_client.get("/api/data_sources", headers=as_(corpus, "owner"))
    assert route.status_code == 200, route.text
    shown = {a["id"]: a for a in route.json()}
    card = _cards(test_client, corpus, "owner", filter="my")[multi]
    assert _agents(card) == {chinook, warehouse}
    for agent in card["agents"]:
        assert agent["icon_token"] is not None
        assert agent["icon_token"] == shown[agent["id"]]["icon_token"], agent
        assert agent["name"] == shown[agent["id"]]["name"]
    tokens = {a["id"]: a["icon_token"] for a in card["agents"]}
    assert tokens[chinook] == "emoji:🎵" and tokens[warehouse] == "type:postgresql"


@pytest.mark.e2e
def test_an_agent_the_agents_page_hides_is_neither_shown_nor_filterable(corpus, corpus_rows, test_client,
                                                                        sqlite_data_source):
    """Public, so visible_data_source_ids has it; but the agents page lists a
    draft only to its managers, a `development` one only to agent admins, and
    a disabled one to nobody -- and so does the card."""
    chinook = corpus.cast.data_sources["chinook"]
    admin = corpus.cast.people["admin"]
    hidden = {}
    for name, fields in (("Draft agent", {"publish_status": "draft"}),
                         ("Development agent", {"reliability_status": "development"}),
                         ("Disabled agent", {"publish_status": "disabled"})):
        hidden[name] = sqlite_data_source(name=name, user_token=admin.token, org_id=corpus.cast.org_id,
                                          is_public=True)["id"]
        _set_agent(test_client, corpus, hidden[name], **fields)
    sales, stock = own(corpus_rows, corpus, SALES), own(corpus_rows, corpus, STOCK)
    for dsid in hidden.values():
        _attach_to_conversation(corpus_rows, corpus.reports[CONV], dsid)
    publish(sales)  # the release takes them too
    _sharing(test_client, corpus, sales, "internal")

    assert _agents(_cards(test_client, corpus, "member", filter="shared")[sales]) == {chinook}
    mine = _cards(test_client, corpus, "owner", filter="my")
    assert _agents(mine[sales]) == {chinook} and _agents(mine[stock]) == {chinook}
    for dsid in hidden.values():
        assert _list(test_client, corpus, "member", filter="shared", agent_ids=dsid)["dashboards"] == []
        assert _list(test_client, corpus, "owner", filter="my", agent_ids=dsid)["dashboards"] == []
    # The org admin manages every agent: the draft and the development one
    # are theirs to see; a disabled agent is off for everyone.
    seen = _cards(test_client, corpus, "admin", filter="shared")
    assert _agents(seen[sales]) == {chinook, hidden["Draft agent"], hidden["Development agent"]}
    assert sales in _cards(test_client, corpus, "admin", filter="shared", agent_ids=hidden["Draft agent"])
    assert _list(test_client, corpus, "admin", filter="shared", agent_ids=hidden["Disabled agent"])["dashboards"] == []


@pytest.mark.e2e
def test_a_draft_lists_the_agents_of_the_conversation_that_wrote_its_newest_version(corpus, corpus_rows,
                                                                                    test_client, spawned):
    chinook, warehouse = corpus.cast.data_sources["chinook"], corpus.cast.data_sources[WAREHOUSE]
    sales, stock = own(corpus_rows, corpus, SALES), own(corpus_rows, corpus, STOCK)
    _grant_editor(test_client, corpus, sales, "member")
    joined = _edit(test_client, corpus, sales, "member").json()["report_id"]
    _finish(spawned)
    assert joined != corpus.reports[CONV]
    # The collaborator's conversation goes on with another agent and writes
    # the newest version.
    _attach_to_conversation(corpus_rows, joined, warehouse)
    new_version(sales, joined, corpus.cast.id("member"))

    mine = _cards(test_client, corpus, "owner", filter="my")
    assert _agents(mine[sales]) == {chinook, warehouse}
    assert _agents(mine[stock]) == {chinook}  # its own conversation's
    on_warehouse = set(_cards(test_client, corpus, "owner", filter="my", agent_ids=warehouse))
    assert sales in on_warehouse and stock not in on_warehouse


@pytest.mark.e2e
def test_a_release_that_is_not_live_lists_the_conversations_agents(corpus, corpus_rows, test_client):
    chinook, warehouse = corpus.cast.data_sources["chinook"], corpus.cast.data_sources[WAREHOUSE]
    sales = own(corpus_rows, corpus, SALES)
    publish(sales, status="failed")  # the release (chinook) never went live
    _attach_to_conversation(corpus_rows, corpus.reports[CONV], warehouse)

    card = _cards(test_client, corpus, "owner", filter="my")[sales]
    assert card["published"] is False and _agents(card) == {chinook, warehouse}
    assert sales in _cards(test_client, corpus, "owner", filter="my", agent_ids=warehouse)


@pytest.mark.e2e
def test_an_agent_without_a_connection_is_listed_without_a_type(corpus, corpus_rows, test_client):
    sales = own(corpus_rows, corpus, SALES)
    bare = _bare_agent(corpus_rows, corpus, "No connection agent")
    _attach_to_conversation(corpus_rows, corpus.reports[CONV], bare)

    agent = next(a for a in _cards(test_client, corpus, "owner", filter="my")[sales]["agents"] if a["id"] == bare)
    assert agent == {"id": bare, "name": "No connection agent", "type": None, "icon_token": None}
    assert sales in _cards(test_client, corpus, "owner", filter="my", agent_ids=bare)


@pytest.mark.e2e
def test_the_icon_follows_the_first_connection_by_creation(corpus, corpus_rows, test_client):
    sales = own(corpus_rows, corpus, SALES)
    # Linked newest first, so neither link order nor insertion order is the
    # creation order.
    two = _bare_agent(corpus_rows, corpus, "Two connections agent",
                      ("mysql", datetime(2026, 2, 1)), ("postgresql", datetime(2026, 1, 1)))
    _attach_to_conversation(corpus_rows, corpus.reports[CONV], two)

    agent = next(a for a in _cards(test_client, corpus, "owner", filter="my")[sales]["agents"] if a["id"] == two)
    assert agent["type"] == "postgresql" and agent["icon_token"] == "type:postgresql"
    route = test_client.get("/api/data_sources", headers=as_(corpus, "owner"))
    assert route.status_code == 200, route.text
    assert {a["id"]: a["icon_token"] for a in route.json()}[two] == agent["icon_token"]


@pytest.mark.e2e
def test_a_cards_agents_are_ordered_by_name_ignoring_case_then_id(corpus, corpus_rows, test_client):
    sales = own(corpus_rows, corpus, SALES)
    made = {}
    for key, name in (("charlie", "Charlie"), ("bravo", "bravo"), ("alpha_upper", "Alpha"), ("alpha", "alpha")):
        made[key] = _bare_agent(corpus_rows, corpus, name)
        _attach_to_conversation(corpus_rows, corpus.reports[CONV], made[key])

    listed = [a["id"] for a in _cards(test_client, corpus, "owner", filter="my")[sales]["agents"]
              if a["id"] in made.values()]
    alphas = sorted([made["alpha_upper"], made["alpha"]])  # the same name but for case: by id
    assert listed == alphas + [made["bravo"], made["charlie"]]
