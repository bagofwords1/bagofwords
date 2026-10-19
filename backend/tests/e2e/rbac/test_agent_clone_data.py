"""Lists, saved queries and evals when cloning an agent.

Contracts:
- Lists: copy = a new empty list with the same fields; link = the same list
  (and rows) on both agents; none = no lists.
- Saved queries: copy = a new query whose code runs on the CLONE's
  connection; its snapshot is not carried over; code reaching ds_clients in
  a way that can't be renamed is left out and reported. With copied lists,
  list ids in the code follow the copies.
- Evals: suites homed on the source come along, re-pointed at the clone;
  archived cases, cases with no agent and cases that don't run on the source
  stay behind; a suite left empty isn't created.
- Mentions: only an @ that resolves to the source (longest name wins) moves
  to the clone; a mention of a table the clone doesn't have stays put.
"""
import uuid

import pytest

from app.services.agent_clone_data import rewrite_client_keys
from tests.e2e.rbac.test_agent_clone import _clone, _h, world  # noqa: F401  (fixture)

pytestmark = pytest.mark.e2e


def _conn_name(test_client, w, ds_id, conn_id):
    ds = test_client.get(f"/api/data_sources/{ds_id}", headers=_h(w["admin"]["token"], w["org_id"])).json()
    return next(c["name"] for c in ds["connections"] if c["id"] == conn_id)


def _entity(test_client, w, code, title):
    r = test_client.post("/api/entities", json={
        "type": "model", "title": title, "slug": f"q-{uuid.uuid4().hex[:8]}", "code": code, "data": {},
        "status": "published", "data_source_ids": [w["agent"]["id"]],
    }, headers=_h(w["admin"]["token"], w["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()


def _lists(test_client, w, ds_id):
    r = test_client.get(f"/api/data_sources/{ds_id}/lists", headers=_h(w["admin"]["token"], w["org_id"]))
    assert r.status_code == 200, r.text
    return {l["name"]: l for l in r.json()}


# ── unit: the code rewrite ─────────────────────────────────────────────────

@pytest.mark.parametrize("before,after", [
    ('ds_clients["Sales:wh"].execute_query("x")', 'ds_clients["Sales copy:wh"].execute_query("x")'),
    ("ds_clients['Sales:wh::fast']", "ds_clients['Sales copy:wh::fast']"),
    ('ds_clients.get("Sales")', 'ds_clients.get("Sales copy")'),
    ('ds_clients["Other:wh"]', 'ds_clients["Other:wh"]'),               # another agent stays
    ('ds_clients["Salesforce:wh"]', 'ds_clients["Salesforce:wh"]'),     # a longer name is not this agent
    ('def generate_df(ds_clients, excel_files, params):\n    return ds_clients["Sales:wh"]',
     'def generate_df(ds_clients, excel_files, params):\n    return ds_clients["Sales copy:wh"]'),
])
def test_client_keys_of_the_source_are_renamed(before, after):
    assert rewrite_client_keys(before, "Sales", "Sales copy") == after


@pytest.mark.parametrize("key", ["Sales: EU:pg", "Sales:other_conn"])
def test_only_the_source_connections_keys_are_renamed(key):
    """Agent names may hold ':'; a key is the source's only for its own connections."""
    code = f'ds_clients["{key}"]'
    assert rewrite_client_keys(code, "Sales", "Sales copy", ["pg", "wh"]) == code
    assert rewrite_client_keys('ds_clients["Sales:pg::fast"]', "Sales", "Sales copy", ["pg"]) == \
        'ds_clients["Sales copy:pg::fast"]'


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


@pytest.mark.parametrize("code", [
    'key = "Sales" + ":wh"\nds_clients[key]',
    "for name, c in ds_clients.items(): pass",
    'ds_clients[f"{agent}:wh"]',
    'def generate_df(ds_clients, excel_files, params):\n    return list(ds_clients.values())[0]',
])
def test_code_that_reaches_clients_other_ways_is_not_rewritten(code):
    assert rewrite_client_keys(code, "Sales", "Sales copy") is None


# ── lists ──────────────────────────────────────────────────────────────────

@pytest.fixture
def with_list(test_client, world):
    r = test_client.post(f"/api/data_sources/{world['agent']['id']}/lists", json={
        "name": "Complaints", "description": "One row per complaint",
        "fields": [{"name": "customer", "type": "string", "required": True}, {"name": "amount", "type": "number"}],
        "key_field": "customer",
    }, headers=_h(world["admin"]["token"], world["org_id"]))
    assert r.status_code == 201, r.text
    world["list"] = r.json()
    return world


def test_lists_are_copied_empty_with_the_same_fields(test_client, with_list):
    w = with_list
    clone = _clone(test_client, w, {"lists": "copy"}).json()
    copy = _lists(test_client, w, clone["data_source"]["id"])["Complaints"]
    assert copy["id"] != w["list"]["id"] and copy["shared"] is False
    assert copy["fields"] == w["list"]["fields"] and copy["slug"] == w["list"]["slug"]
    assert copy["row_count"] == 0
    assert clone["summary"]["lists"]["copied"] == 1
    assert _lists(test_client, w, w["agent"]["id"])["Complaints"]["shared"] is False


def test_lists_can_be_shared_with_the_clone(test_client, with_list):
    w = with_list
    clone = _clone(test_client, w, {"lists": "link"}).json()
    shared = _lists(test_client, w, clone["data_source"]["id"])["Complaints"]
    assert shared["id"] == w["list"]["id"] and shared["shared"] is True
    assert clone["summary"]["lists"]["linked"] == 1


def test_lists_can_be_left_out(test_client, with_list):
    clone = _clone(test_client, with_list, {"lists": "none"}).json()
    assert _lists(test_client, with_list, clone["data_source"]["id"]) == {}


# ── saved queries ──────────────────────────────────────────────────────────

def test_a_copied_saved_query_runs_on_the_clone(test_client, world):
    w = world
    key = f'{w["agent"]["name"]}:{_conn_name(test_client, w, w["agent"]["id"], w["sql"])}'
    code = (
        "def generate_df(ds_clients, excel_files, params):\n"
        f'    return ds_clients["{key}"].execute_query("select count(*) as n from Artist")\n'
    )
    src = _entity(test_client, w, code, "Artist count")
    h = _h(w["admin"]["token"], w["org_id"])
    assert test_client.post(f"/api/entities/{src['id']}/run", json={}, headers=h).status_code == 200

    body = _clone(test_client, w, {"name": f"Copy {uuid.uuid4().hex[:4]}", "saved_queries": "copy"}).json()
    clone = body["data_source"]
    assert body["summary"]["saved_queries"]["copied"] == 1
    copies = [e for e in test_client.get("/api/entities", params={"data_source_ids": clone["id"]}, headers=h).json()
              if e["title"] == "Artist count" and e["id"] != src["id"]]
    assert len(copies) == 1
    copy = test_client.get(f"/api/entities/{copies[0]['id']}", headers=h).json()
    assert f'"{clone["name"]}:' in copy["code"] and f'"{w["agent"]["name"]}:' not in copy["code"]
    assert not (copy.get("data") or {}).get("rows")  # the source's snapshot stays with the source
    assert [d["id"] for d in copy["data_sources"]] == [clone["id"]]

    ran = test_client.post(f"/api/entities/{copy['id']}/run", json={}, headers=h)
    assert ran.status_code == 200, ran.text
    assert ran.json()["data"]["rows"][0]["n"] > 0
    # The source is untouched.
    assert test_client.get(f"/api/entities/{src['id']}", headers=h).json()["code"] == code


def test_copied_saved_queries_get_slugs_of_their_own(test_client, world):
    """Slugs are kept unique per organization (EntityService._unique_slug),
    but the column isn't: older rows can share one. Copies never collide with
    each other or with what exists."""
    import asyncio

    from sqlalchemy import update

    from app.dependencies import async_session_maker
    from app.models.entity import Entity

    w = world
    h = _h(w["admin"]["token"], w["org_id"])
    base = f"rev-{uuid.uuid4().hex[:6]}"
    srcs = []
    for slug in (base, f"{base}-x"):
        r = test_client.post("/api/entities", json={
            "type": "model", "title": "Revenue", "slug": slug, "code": "x = 1", "data": {},
            "status": "published", "data_source_ids": [w["agent"]["id"]],
        }, headers=h)
        assert r.status_code == 200, r.text
        srcs.append(r.json())

    async def _same_slug():
        async with async_session_maker() as db:
            await db.execute(update(Entity).where(Entity.id == srcs[1]["id"]).values(slug=base))
            await db.commit()
    asyncio.run(_same_slug())
    srcs[1]["slug"] = base
    clone = _clone(test_client, w, {"saved_queries": "copy"}).json()["data_source"]
    copies = [e for e in test_client.get("/api/entities", params={"data_source_ids": clone["id"]}, headers=h).json()
              if e["id"] not in {s["id"] for s in srcs}]
    slugs = sorted(e["slug"] for e in copies)
    assert len(slugs) == 2 and len(set(slugs)) == 2
    assert not set(slugs) & {s["slug"] for s in srcs}
    assert all(s.startswith(base) for s in slugs)


def test_a_saved_query_that_cannot_be_renamed_is_left_out_and_reported(test_client, world):
    w = world
    code = ("def generate_df(ds_clients, excel_files, params):\n"
            "    client = list(ds_clients.values())[0]\n"
            '    return client.execute_query("select 1 as n")\n')
    src = _entity(test_client, w, code, "Opaque query")
    body = _clone(test_client, w, {"saved_queries": "copy"}).json()
    skipped = body["summary"]["saved_queries"]["skipped"]
    assert [s["id"] for s in skipped] == [src["id"]] and skipped[0]["name"] == "Opaque query"
    assert body["summary"]["saved_queries"]["copied"] == 0


def test_saved_queries_follow_copied_lists(test_client, with_list):
    w = with_list
    code = ("def generate_df(ds_clients, excel_files, params):\n"
            f'    return ds_clients["bow"].execute_query({{"dataset": "list", "list_id": "{w["list"]["id"]}"}})\n')
    _entity(test_client, w, code, "Complaints rows")
    h = _h(w["admin"]["token"], w["org_id"])
    for lists, expect_new in (("copy", True), ("link", False)):
        clone = _clone(test_client, w, {"lists": lists, "saved_queries": "copy"}).json()["data_source"]
        copy_list = _lists(test_client, w, clone["id"])["Complaints"]["id"]
        q = next(e for e in test_client.get("/api/entities", params={"data_source_ids": clone["id"]}, headers=h).json()
                 if e["title"] == "Complaints rows" and clone["id"] in [d["id"] for d in e["data_sources"]])
        full = test_client.get(f"/api/entities/{q['id']}", headers=h).json()
        assert (copy_list in full["code"]) and ((w["list"]["id"] in full["code"]) is not expect_new)


def test_saved_queries_follow_a_copied_list_named_after_another_agent(test_client, world, sqlite_data_source):
    """A list shared with the source but created on another agent is named
    after that agent in code (bow.<home>.lists.<slug>); the copy follows too."""
    from app.models.agent_list import AgentList
    from app.models.data_source import DataSource
    from app.services.agent_lists import service as svc
    from app.services.agent_lists.naming import table_name_for
    from tests.e2e.rbac.test_agent_lists import run_async

    w = world
    h = _h(w["admin"]["token"], w["org_id"])
    other = sqlite_data_source(name=f"Legal {uuid.uuid4().hex[:4]}", user_token=w["admin"]["token"], org_id=w["org_id"])
    lst = test_client.post(f"/api/data_sources/{other['id']}/lists", json={
        "name": "Contracts", "fields": [{"name": "party", "type": "string"}]}, headers=h).json()

    async def _link(db, maker):  # no route links a list yet (agent clone is its caller)
        await svc.link_list(db, await db.get(AgentList, lst["id"]), await db.get(DataSource, w["agent"]["id"]))
    run_async(_link)
    home_name = table_name_for(other["name"], other["id"], lst["slug"])
    code = ("def generate_df(ds_clients, excel_files, params):\n"
            f'    return ds_clients["bow"].execute_query({{"dataset": "list", "list": "{home_name}"}})\n')
    _entity(test_client, w, code, "Contracts rows")
    clone = _clone(test_client, w, {"lists": "copy", "saved_queries": "copy"}).json()["data_source"]
    q = next(e for e in test_client.get("/api/entities", params={"data_source_ids": clone["id"]}, headers=h).json()
             if e["title"] == "Contracts rows" and clone["id"] in [d["id"] for d in e["data_sources"]])
    full = test_client.get(f"/api/entities/{q['id']}", headers=h).json()
    assert home_name not in full["code"]
    assert table_name_for(clone["name"], clone["id"], lst["slug"]) in full["code"]


# ── evals ──────────────────────────────────────────────────────────────────

def test_evals_homed_on_the_source_come_along_re_pointed_at_the_clone(test_client, world):
    w = world
    h = _h(w["admin"]["token"], w["org_id"])
    suite = test_client.post("/api/tests/suites", json={"name": "Sales checks", "data_source_id": w["agent"]["id"]},
                             headers=h).json()
    spec = {"spec_version": 1, "rules": [], "order_mode": "flexible"}
    mention = {"type": "data_source", "id": w["agent"]["id"], "name": w["agent"]["name"]}

    def _case(name, ds_ids, content="How many artists?", mentions=None):
        r = test_client.post(f"/api/tests/suites/{suite['id']}/cases", json={
            "name": name, "prompt_json": {"content": content, "mentions": mentions or []},
            "expectations_json": spec, "data_source_ids_json": ds_ids,
        }, headers=h)
        assert r.status_code == 200, r.text
        return r.json()
    _case("Pinned", [w["agent"]["id"]], content=f'@"{w["agent"]["name"]}" how many artists?', mentions=[mention])
    _case("Everywhere", [])
    old = _case("Old", [w["agent"]["id"]])
    assert test_client.patch(f"/api/tests/cases/{old['id']}/status", json={"status": "archived"}, headers=h).status_code == 200

    body = _clone(test_client, w, {"evals": "copy"}).json()
    clone = body["data_source"]
    suites = [s for s in test_client.get("/api/tests/suites", params={"data_source_id": clone["id"]}, headers=h).json()
              if s["name"] == "Sales checks" and s["id"] != suite["id"]]
    assert len(suites) == 1 and suites[0]["data_source_id"] == clone["id"]
    cases = test_client.get(f"/api/tests/suites/{suites[0]['id']}/cases", headers=h).json()
    assert [c["name"] for c in cases] == ["Pinned"]
    pinned = cases[0]
    assert pinned["data_source_ids_json"] == [clone["id"]]
    assert pinned["prompt_json"]["mentions"][0]["id"] == clone["id"]
    assert clone["name"] in pinned["prompt_json"]["content"]
    assert {s["name"] for s in body["summary"]["evals"]["skipped"]} == {"Everywhere", "Old"}
    # The source keeps its suite and all three cases.
    assert len(test_client.get(f"/api/tests/suites/{suite['id']}/cases", headers=h).json()) == 3


def test_follow_up_turns_of_an_eval_point_at_the_clone():
    """Multi-turn cases come from YAML import (the API takes one turn); each
    follow-up is {"prompt": {...}} and must be re-pointed like the first."""
    from types import SimpleNamespace

    from app.services.agent_clone_data import _remap_turn
    from app.utils.mentions import MentionMatcher
    ctx = SimpleNamespace(source=SimpleNamespace(name="Sales"), clone=SimpleNamespace(name="Sales copy"),
                          source_id="src", clone_id="dst", table_map={}, entity_map={},
                          mention_matcher=MentionMatcher(["Sales"]))
    turn = {"prompt": {"content": "now ask @Sales for Q2", "step_id": "s1", "widget_id": "w1",
                       "mentions": [{"type": "data_source", "id": "src", "name": "Sales"}]}, "expectations": {}}
    out = _remap_turn(turn, ctx)["prompt"]
    assert out["content"] == 'now ask @"Sales copy" for Q2'
    assert out["mentions"][0]["id"] == "dst" and out["step_id"] is None and out["widget_id"] is None


def test_eval_cases_that_dont_run_on_the_source_stay_behind(test_client, world, sqlite_data_source):
    """A case pinned to other agents only would run exactly as the original
    does: copying it is a duplicate. A suite left with no case isn't created."""
    w = world
    h = _h(w["admin"]["token"], w["org_id"])
    other = sqlite_data_source(name=f"Legal {uuid.uuid4().hex[:4]}", user_token=w["admin"]["token"], org_id=w["org_id"])
    spec = {"spec_version": 1, "rules": [], "order_mode": "flexible"}

    def _suite(name, cases):
        s = test_client.post("/api/tests/suites", json={"name": name, "data_source_id": w["agent"]["id"]},
                             headers=h).json()
        for case_name, ds_ids in cases:
            r = test_client.post(f"/api/tests/suites/{s['id']}/cases", json={
                "name": case_name, "prompt_json": {"content": "How many artists?", "mentions": []},
                "expectations_json": spec, "data_source_ids_json": ds_ids,
            }, headers=h)
            assert r.status_code == 200, r.text
        return s
    _suite("Mixed", [("Both", [w["agent"]["id"], other["id"]]), ("Legal only", [other["id"]])])
    _suite("Legal checks", [("Legal again", [other["id"]])])

    body = _clone(test_client, w, {"evals": "copy"}).json()
    clone = body["data_source"]
    suites = {s["name"]: s for s in test_client.get("/api/tests/suites", params={"data_source_id": clone["id"]},
                                                    headers=h).json() if s["data_source_id"] == clone["id"]}
    assert set(suites) == {"Mixed"}
    cases = test_client.get(f"/api/tests/suites/{suites['Mixed']['id']}/cases", headers=h).json()
    assert [(c["name"], sorted(c["data_source_ids_json"])) for c in cases] == [("Both", sorted([clone["id"], other["id"]]))]
    assert {s["name"] for s in body["summary"]["evals"]["skipped"]} == {"Legal only", "Legal again", "Legal checks"}


def test_a_mention_of_a_table_the_clone_doesnt_have_stays_on_the_source():
    from types import SimpleNamespace

    from app.services.agent_clone_service import remap_mentions
    ctx = SimpleNamespace(source_id="src", clone_id="dst", clone=SimpleNamespace(name="Sales copy"),
                          table_map={"t1": "t1-copy"}, entity_map={})
    copied = {"type": "datasource_table", "id": "t1", "datasource_id": "src", "data_source_name": "Sales"}
    users = {"type": "datasource_table", "id": "t-user", "datasource_id": "src", "data_source_name": "Sales"}
    assert remap_mentions(copied, ctx) == {**copied, "id": "t1-copy", "datasource_id": "dst",
                                           "data_source_name": "Sales copy"}
    assert remap_mentions(users, ctx) == users
