"""Agent Lists shared by several agents.

Contracts:
- A list linked to several agents is ONE list: every linked agent shows it,
  fills it and reads the same rows.
- Reading needs VIEW on one linked agent; the payload names only the linked
  agents the caller can see.
- Rows: MANAGE on the agent you work from. Schema, and deleting the list from
  every agent: MANAGE on every linked agent (API and the edit_list tool).
- Removing a list from one agent keeps it, with its rows, on the others.
- Deleting an agent keeps the lists it shared with other agents.
- A conversation with several linked agents gets one submit tool and one
  bow table for the list; the bow name of any linked agent resolves to it.
- Linking refuses a list whose name or machine name is already used on the
  target agent.
"""
import uuid

import pytest
from fastapi import HTTPException

from tests.e2e.rbac.test_agent_lists import (  # shared API-seeded list world
    _build,
    _h,
    _query,
    _record,
    _schema,
    run_async,
    submit,
    world,  # noqa: F401  (fixture)
)

pytestmark = pytest.mark.e2e


def _link(world, list_id, agent_key="other_agent"):
    """Link through the service: in this release a list is linked to another
    agent only by cloning an agent, which has no route of its own yet."""
    from app.models.agent_list import AgentList
    from app.models.data_source import DataSource
    from app.services.agent_lists import service as svc

    async def _fn(db, maker):
        lst = await db.get(AgentList, list_id)
        ds = await db.get(DataSource, world[agent_key]["id"])
        await svc.link_list(db, lst, ds)
    return run_async(_fn)


def _lists(test_client, world, agent_key, actor="admin"):
    r = test_client.get(f"/api/data_sources/{world[agent_key]['id']}/lists",
                        headers=_h(world[actor]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    return {l["id"]: l for l in r.json()}


def _rows_total(test_client, world, agent_key, list_id, actor="admin"):
    r = test_client.get(f"/api/data_sources/{world[agent_key]['id']}/lists/{list_id}/rows",
                        headers=_h(world[actor]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()["total"]


@pytest.fixture
def shared(world, create_report):
    _link(world, world["list"]["id"])
    world["both"] = create_report(title="Both", user_token=world["admin"]["token"], org_id=world["org_id"],
                                  data_sources=[world["agent"]["id"], world["other_agent"]["id"]])
    return world


def test_linked_list_is_one_list_on_both_agents(test_client, shared):
    w = shared
    lid = w["list"]["id"]
    on_other = _lists(test_client, w, "other_agent")[lid]
    on_home = _lists(test_client, w, "agent")[lid]
    for view, agent_key in ((on_other, "other_agent"), (on_home, "agent")):
        assert view["shared"] is True
        assert {a["id"] for a in view["agents"]} == {w["agent"]["id"], w["other_agent"]["id"]}
        assert view["data_source_id"] == w[agent_key]["id"]
        assert view["fields"] == w["list"]["fields"]

    # Saved through the other agent's conversation, read from the home agent.
    out = submit(w, [_record(cp="Initech"), _record(cp="Hooli")], report_key="other_report")
    assert out["output"]["success"] is True, out
    assert out["output"]["data_source_id"] == w["other_agent"]["id"]
    assert _rows_total(test_client, w, "agent", lid) == 2
    assert _rows_total(test_client, w, "other_agent", lid) == 2

    counts = test_client.get("/api/agent_lists/counts", headers=_h(w["admin"]["token"], w["org_id"])).json()
    assert counts["by_agent"][w["agent"]["id"]] >= 1 and counts["by_agent"][w["other_agent"]["id"]] >= 1


def test_unlinked_agent_does_not_see_the_list(test_client, world):
    assert world["list"]["id"] not in _lists(test_client, world, "other_agent")
    r = test_client.get(f"/api/data_sources/{world['other_agent']['id']}/lists/{world['list']['id']}",
                        headers=_h(world["admin"]["token"], world["org_id"]))
    assert r.status_code == 404
    out = submit(world, [_record()], report_key="other_report")
    assert out["output"]["success"] is False


def test_payload_names_only_the_agents_the_caller_can_see(test_client, shared):
    w = shared
    view = _lists(test_client, w, "agent", actor="viewer")[w["list"]["id"]]
    assert view["shared"] is True
    assert [a["id"] for a in view["agents"]] == [w["agent"]["id"]]
    assert view["hidden_agent_count"] == 1
    assert w["other_agent"]["name"] not in str(view)


def test_schema_change_needs_manage_on_every_linked_agent(test_client, shared):
    w = shared
    lid = w["list"]["id"]
    body = {**_schema(), "description": f"changed {uuid.uuid4().hex[:6]}", "fields": w["list"]["fields"]}

    as_manager = _lists(test_client, w, "agent", actor="manager")[lid]
    assert as_manager["can_manage"] is True and as_manager["can_manage_schema"] is False
    r = test_client.put(f"/api/data_sources/{w['agent']['id']}/lists/{lid}", json=body,
                        headers=_h(w["manager"]["token"], w["org_id"]))
    assert r.status_code == 403

    # Rows stay editable from the agent they manage.
    submit(w, [_record(cp="Umbrella")])
    row = test_client.get(f"/api/data_sources/{w['agent']['id']}/lists/{lid}/rows",
                          headers=_h(w["manager"]["token"], w["org_id"])).json()["rows"][0]
    r = test_client.delete(f"/api/data_sources/{w['agent']['id']}/lists/{lid}/rows/{row['id']}",
                           headers=_h(w["manager"]["token"], w["org_id"]))
    assert r.status_code == 204

    assert _lists(test_client, w, "agent")[lid]["can_manage_schema"] is True
    r = test_client.put(f"/api/data_sources/{w['agent']['id']}/lists/{lid}", json=body,
                        headers=_h(w["admin"]["token"], w["org_id"]))
    assert r.status_code == 200, r.text
    assert _lists(test_client, w, "other_agent")[lid]["description"] == body["description"]


def test_unshared_list_schema_stays_editable_by_its_agent_manager(test_client, world):
    lid = world["list"]["id"]
    assert _lists(test_client, world, "agent", actor="manager")[lid]["can_manage_schema"] is True
    body = {**_schema(), "description": "edited by manager", "fields": world["list"]["fields"]}
    r = test_client.put(f"/api/data_sources/{world['agent']['id']}/lists/{lid}", json=body,
                        headers=_h(world["manager"]["token"], world["org_id"]))
    assert r.status_code == 200, r.text


def test_removing_from_one_agent_keeps_the_list_and_rows_on_the_other(test_client, shared):
    w = shared
    lid = w["list"]["id"]
    submit(w, [_record(cp="Stark"), _record(cp="Wayne")])
    # The home agent's manager may take the list off that agent only.
    r = test_client.delete(f"/api/data_sources/{w['agent']['id']}/lists/{lid}", params={"scope": "agent"},
                           headers=_h(w["manager"]["token"], w["org_id"]))
    assert r.status_code == 204
    assert lid not in _lists(test_client, w, "agent")
    remaining = _lists(test_client, w, "other_agent")[lid]
    assert remaining["shared"] is False
    assert _rows_total(test_client, w, "other_agent", lid) == 2


def test_removing_the_last_agent_deletes_the_list(test_client, world):
    lid = world["list"]["id"]
    r = test_client.delete(f"/api/data_sources/{world['agent']['id']}/lists/{lid}", params={"scope": "agent"},
                           headers=_h(world["admin"]["token"], world["org_id"]))
    assert r.status_code == 204
    assert lid not in _lists(test_client, world, "agent")
    df = None
    with pytest.raises(ValueError):
        df = _query(world, {"dataset": "list", "list_id": lid})
    assert df is None


def test_delete_everywhere_needs_manage_on_every_linked_agent(test_client, shared):
    w = shared
    lid = w["list"]["id"]
    r = test_client.delete(f"/api/data_sources/{w['agent']['id']}/lists/{lid}", params={"scope": "all"},
                           headers=_h(w["manager"]["token"], w["org_id"]))
    assert r.status_code == 403
    assert lid in _lists(test_client, w, "other_agent")

    r = test_client.delete(f"/api/data_sources/{w['agent']['id']}/lists/{lid}",
                           headers=_h(w["admin"]["token"], w["org_id"]))
    assert r.status_code == 204
    assert lid not in _lists(test_client, w, "agent")
    assert lid not in _lists(test_client, w, "other_agent")


def test_deleting_an_agent_keeps_the_lists_it_shared(test_client, shared):
    w = shared
    lid = w["list"]["id"]
    submit(w, [_record(cp="Tyrell"), _record(cp="Cyberdyne"), _record(cp="Soylent")])
    r = test_client.delete(f"/api/data_sources/{w['agent']['id']}", headers=_h(w["admin"]["token"], w["org_id"]))
    assert r.status_code in (200, 204), r.text

    survivor = _lists(test_client, w, "other_agent")[lid]
    assert survivor["shared"] is False
    assert [a["id"] for a in survivor["agents"]] == [w["other_agent"]["id"]]
    assert _rows_total(test_client, w, "other_agent", lid) == 3

    # The list now lives on the remaining agent alone. Read from the model:
    # SQLite does not enforce the agent's CASCADE, so the API above would look
    # the same even if the deleted agent's link and home were left behind.
    from app.models.agent_list import AgentList
    from app.services.agent_lists.service import linked_agent_ids

    async def _state(db, maker):
        lst = await db.get(AgentList, lid)
        return str(lst.data_source_id), (await linked_agent_ids(db, [lid]))[lid]
    home, linked = run_async(_state)
    assert home == w["other_agent"]["id"]
    assert linked == [w["other_agent"]["id"]]


def test_one_submit_tool_and_one_table_per_shared_list(shared):
    w = shared
    descriptors, routing = _build(w, report_key="both")
    for_list = [n for n, v in routing.items() if v["list_id"] == w["list"]["id"]]
    assert for_list == ["submit_contracts"]
    assert len(descriptors) == len(routing)

    from app.models.organization import Organization
    from app.models.user import User
    from app.services.agent_lists.bow_lists import list_tables

    async def _tables(db, maker):
        user = await db.get(User, w["admin"]["user_id"])
        org = await db.get(Organization, w["org_id"])
        return await list_tables(db, user, org, [w["agent"]["id"], w["other_agent"]["id"]])
    entries = [e for e in run_async(_tables) if str(e["list"].id) == w["list"]["id"]]
    assert len(entries) == 1
    assert {str(a.id) for a in entries[0]["agents"]} == {w["agent"]["id"], w["other_agent"]["id"]}


def test_bow_name_of_either_linked_agent_resolves_the_shared_list(test_client, shared):
    from app.services.agent_lists.naming import table_name_for

    w = shared
    lid = w["list"]["id"]
    submit(w, [_record(cp="Oscorp")])
    # Every page names a shared table alike, after the agent the list was
    # created on. A second list created on the other agent covers both id
    # orders of home vs. the other linked agent.
    r = test_client.post(f"/api/data_sources/{w['other_agent']['id']}/lists", json=_schema(name="Renewals"),
                         headers=_h(w["admin"]["token"], w["org_id"]))
    assert r.status_code == 201, r.text
    second = r.json()
    _link(w, second["id"], agent_key="agent")
    for one in (w["list"], second):
        shown = {_lists(test_client, w, k)[one["id"]]["table_name"] for k in ("agent", "other_agent")}
        assert shown == {one["table_name"]}
    # ...and the name built from either linked agent finds it.
    for k in ("agent", "other_agent"):
        name = table_name_for(w[k]["name"], w[k]["id"], w["list"]["slug"])
        df = _query(w, {"dataset": "list", "list": name})
        assert list(df["counterparty"]) == ["Oscorp"]


def test_reading_needs_view_on_one_linked_agent(test_client, shared, grant_resource):
    w = shared
    lid = w["list"]["id"]
    with pytest.raises(PermissionError):
        _query(w, {"dataset": "list", "list_id": lid}, actor="outsider")
    r = grant_resource(resource_type="data_source", resource_id=w["other_agent"]["id"], principal_type="user",
                       principal_id=w["outsider"]["user_id"], permissions=["view"],
                       user_token=w["admin"]["token"], org_id=w["org_id"])
    assert r.status_code in (200, 201), r.text
    assert len(_query(w, {"dataset": "list", "list_id": lid}, actor="outsider").columns) > 0


@pytest.mark.parametrize("taken_name", [
    "CONTRACTS",    # same name (names compare case-insensitively)
    "Contracts!",   # different name, same machine name (slug "contracts")
])
def test_linking_refuses_a_name_or_machine_name_taken_on_the_target(test_client, world, taken_name):
    h = _h(world["admin"]["token"], world["org_id"])
    r = test_client.post(f"/api/data_sources/{world['other_agent']['id']}/lists",
                         json=_schema(name=taken_name), headers=h)
    assert r.status_code == 201 and r.json()["slug"] == "contracts", r.text
    with pytest.raises(HTTPException) as exc:
        _link(world, world["list"]["id"])
    assert exc.value.status_code == 409
    assert world["list"]["id"] not in _lists(test_client, world, "other_agent")


def test_edit_list_tool_refuses_a_list_shared_with_agents_the_user_does_not_manage(shared):
    w = shared
    from app.ai.tools.implementations.edit_list import EditListTool
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    async def _fn(db, maker):
        report = await db.get(Report, w["report"]["id"])
        user = await db.get(User, w["manager"]["user_id"])
        org = await db.get(Organization, w["org_id"])
        ctx = {"db": db, "session_maker": maker, "report": report, "user": user, "organization": org,
               "tool_call_id": str(uuid.uuid4())}
        events = [e async for e in EditListTool().run_stream(
            {"list_id": w["list"]["id"], "description": "new description", "reason": "test"}, ctx)]
        return [e for e in events if e.type == "tool.end"][-1].payload
    out = run_async(_fn)
    assert out["output"]["success"] is False
    assert out["output"]["status"] == "permission_denied"


def test_clearing_a_shared_list_needs_manage_on_every_linked_agent(test_client, shared):
    w = shared
    lid = w["list"]["id"]
    submit(w, [_record(cp="Acme"), _record(cp="Globex")], report_key="other_report")
    url = f"/api/data_sources/{w['agent']['id']}/lists/{lid}/rows/delete"
    r = test_client.post(url, json={"all": True}, headers=_h(w["manager"]["token"], w["org_id"]))
    assert r.status_code == 403
    assert _rows_total(test_client, w, "other_agent", lid) == 2

    # Picking rows one by one stays with the agent's manager.
    rows = test_client.get(f"/api/data_sources/{w['agent']['id']}/lists/{lid}/rows",
                           headers=_h(w["manager"]["token"], w["org_id"])).json()["rows"]
    r = test_client.post(url, json={"row_ids": [rows[0]["id"]]}, headers=_h(w["manager"]["token"], w["org_id"]))
    assert r.status_code == 200 and r.json()["deleted"] == 1

    r = test_client.post(url, json={"all": True}, headers=_h(w["admin"]["token"], w["org_id"]))
    assert r.status_code == 200 and r.json()["deleted"] == 1
    assert _rows_total(test_client, w, "other_agent", lid) == 0


def test_unshared_list_can_be_cleared_by_its_agent_manager(test_client, world):
    submit(world, [_record(cp="Acme")])
    r = test_client.post(f"/api/data_sources/{world['agent']['id']}/lists/{world['list']['id']}/rows/delete",
                         json={"all": True}, headers=_h(world["manager"]["token"], world["org_id"]))
    assert r.status_code == 200 and r.json()["deleted"] == 1


def test_preparing_an_agent_delete_leaves_its_own_lists_untouched(test_client, world):
    """If the agent delete fails after this step, lists on that agent alone
    must still be there."""
    from app.services.agent_lists.service import detach_agent_lists

    async def _detach(db, maker):
        await detach_agent_lists(db, world["agent"]["id"])
        await db.commit()
    run_async(_detach)
    assert world["list"]["id"] in _lists(test_client, world, "agent")


def test_submit_tool_names_the_table_the_schema_context_shows(test_client, shared, grant_resource, create_report):
    """The tool is routed through an agent the user may save through, which
    need not be the agent the table is named after; both must name it alike.
    Here the user views the list's home agent and manages the other one, so
    the routing agent and the naming agent always differ."""
    w = shared
    low, high = w["agent"]["id"], w["other_agent"]["id"]  # home, other (names kept for the asserts below)
    for ds_id, perms in ((low, ["view"]), (high, ["manage"])):
        r = grant_resource(resource_type="data_source", resource_id=ds_id, principal_type="user",
                           principal_id=w["outsider"]["user_id"], permissions=perms,
                           user_token=w["admin"]["token"], org_id=w["org_id"])
        assert r.status_code in (200, 201), r.text
    w["outsider_both"] = create_report(title="Outsider both", user_token=w["outsider"]["token"], org_id=w["org_id"],
                                       data_sources=[low, high])

    from app.models.organization import Organization
    from app.models.user import User
    from app.services.agent_lists.bow_lists import list_tables

    async def _tables(db, maker):
        user = await db.get(User, w["outsider"]["user_id"])
        org = await db.get(Organization, w["org_id"])
        return await list_tables(db, user, org, [low, high])
    table = next(e["name"] for e in run_async(_tables) if str(e["list"].id) == w["list"]["id"])
    descriptors, routing = _build(w, actor="outsider", report_key="outsider_both")
    name = next(n for n, v in routing.items() if v["list_id"] == w["list"]["id"])
    assert routing[name]["data_source_id"] == high
    description = next(d["description"] for d in descriptors if d["name"] == name)
    assert table in description
    # ...and the list pages show that same name, from either agent.
    for ds_id in (low, high):
        r = test_client.get(f"/api/data_sources/{ds_id}/lists/{w['list']['id']}",
                            headers=_h(w["outsider"]["token"], w["org_id"]))
        assert r.status_code == 200 and r.json()["table_name"] == table, r.text


def test_a_deleted_list_cannot_be_linked(test_client, world):
    r = test_client.delete(f"/api/data_sources/{world['agent']['id']}/lists/{world['list']['id']}",
                           headers=_h(world["admin"]["token"], world["org_id"]))
    assert r.status_code == 204
    with pytest.raises(HTTPException) as exc:
        _link(world, world["list"]["id"])
    assert exc.value.status_code == 404
    assert world["list"]["id"] not in _lists(test_client, world, "other_agent")
