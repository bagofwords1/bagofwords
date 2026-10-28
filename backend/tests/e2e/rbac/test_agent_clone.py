"""Cloning an agent (POST /data_sources/{id}/clone).

Contracts:
- The clone is a new agent on the SAME connections, with the source's
  settings, table selection (hidden columns included), tool policies and
  agent prompts. Like a new agent it starts in Training, managed by the
  cloner, and it is live, draft or disabled as the source is.
- Files follow the choice: shared (the same file on both agents) or left out.
- Access follows the choice: the source's user/group members and their
  permissions, or only the cloner (as for a new agent).
- Cloning needs manage on the source AND the right to build an agent on its
  connections; a taken name is a 409; nothing is created when it fails.
- The source is unchanged.
"""
import io
import uuid

import pytest

from tests.e2e.test_agent_teardown_dependents import _sqlite_connection

pytestmark = pytest.mark.e2e


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _clone(test_client, world, body=None, actor="admin", source_key="agent"):
    payload = {"name": f"Clone {uuid.uuid4().hex[:6]}", **(body or {})}
    return test_client.post(f"/api/data_sources/{world[source_key]['id']}/clone", json=payload,
                            headers=_h(world[actor]["token"], world["org_id"]))


def _tables(test_client, world, ds_id, actor="admin"):
    r = test_client.get(f"/api/data_sources/{ds_id}/full_schema", params={"page": 1, "page_size": 500},
                        headers=_h(world[actor]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    return {t["name"]: t for t in r.json()["tables"]}


def _agent_ids(test_client, world, actor="admin"):
    r = test_client.get("/api/data_sources", headers=_h(world[actor]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    return {d["id"] for d in r.json()}


def _tools(test_client, world, ds_id):
    r = test_client.get(f"/api/data_sources/{ds_id}/tools", headers=_h(world["admin"]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    return {t["id"]: t for t in r.json()}


@pytest.fixture
def world(test_client, bootstrap_admin, invite_user_to_org, grant_resource, create_custom_api_connection,
          update_tables_status_delta, refresh_schema):
    admin = bootstrap_admin("admin")
    org_id, token = admin["org_id"], admin["token"]
    h = _h(token, org_id)
    sql = _sqlite_connection(test_client, token, org_id, f"wh-{uuid.uuid4().hex[:4]}")
    api = create_custom_api_connection(user_token=token, org_id=org_id, endpoints=[
        {"name": f"get_{n}", "method": "GET", "path": f"/{n}", "description": n, "parameters": []}
        for n in ("orders", "refunds")
    ])
    r = test_client.post("/api/data_sources", json={
        "name": f"Sales Ops {uuid.uuid4().hex[:4]}", "connection_ids": [sql, api["id"]],
        "description": "Sales questions", "context": "Fiscal year starts in April.",
    }, headers=h)
    assert r.status_code == 200, r.text
    agent = r.json()
    r = test_client.put(f"/api/data_sources/{agent['id']}", json={
        "icon": "emoji:📊", "conversation_starters": ["Top customers this month?"],
        "context": "Fiscal year starts in April.",
    }, headers=h)
    assert r.status_code == 200, r.text

    # A table selection with hidden columns, and one tool denied. Indexing the
    # connection runs in the background; sync now so the tables are there.
    refresh_schema(data_source_id=agent["id"], user_token=token, org_id=org_id)
    tables = _tables(test_client, {"admin": admin, "org_id": org_id}, agent["id"])
    assert tables, "the agent has no tables to clone"
    picked = sorted(tables)[:2]
    target = tables[picked[0]]
    hidden = [c["name"] for c in target["columns"]][1:2]
    update_tables_status_delta(data_source_id=agent["id"], activate=[tables[n]["id"] for n in picked],
                               excluded_columns={target["id"]: hidden}, user_token=token, org_id=org_id)
    api_tools = [t for t in _tools(test_client, {"admin": admin, "org_id": org_id}, agent["id"]).values()
                 if t["connection_id"] == api["id"]]
    denied = api_tools[0]
    r = test_client.put(f"/api/data_sources/{agent['id']}/tools/{denied['id']}", json={"policy": "deny"}, headers=h)
    assert r.status_code == 200, r.text

    viewer = invite_user_to_org(org_id=org_id, admin_token=token)
    manager = invite_user_to_org(org_id=org_id, admin_token=token)
    for who, perms in ((viewer, ["view"]), (manager, ["manage"])):
        r = grant_resource(resource_type="data_source", resource_id=agent["id"], principal_type="user",
                           principal_id=who["user_id"], permissions=perms, user_token=token, org_id=org_id)
        assert r.status_code in (200, 201), r.text
    return {"org_id": org_id, "admin": admin, "viewer": viewer, "manager": manager, "agent": agent,
            "sql": sql, "api": api["id"], "picked": picked, "hidden": hidden, "target": picked[0],
            "denied_tool": denied["id"]}


def test_clone_carries_settings_connections_tables_and_tool_policies(test_client, world):
    r = _clone(test_client, world, {"name": "Sales Ops copy", "description": "For the finance team"})
    assert r.status_code == 201, r.text
    clone = r.json()["data_source"]
    src = test_client.get(f"/api/data_sources/{world['agent']['id']}",
                          headers=_h(world["admin"]["token"], world["org_id"])).json()

    assert clone["id"] != src["id"]
    assert clone["name"] == "Sales Ops copy" and clone["description"] == "For the finance team"
    for key in ("context", "icon", "conversation_starters"):
        assert clone[key] == src[key], key
    assert clone["publish_status"] == "published" and clone["reliability_status"] == "training"
    assert clone["owner_user_id"] == world["admin"]["user_id"]
    assert {c["id"] for c in clone["connections"]} == {c["id"] for c in src["connections"]}

    src_t, clone_t = _tables(test_client, world, src["id"]), _tables(test_client, world, clone["id"])
    assert set(clone_t) == set(src_t)
    assert {n for n, t in clone_t.items() if t["is_active"]} == set(world["picked"])
    assert sorted(clone_t[world["target"]]["excluded_columns"]) == sorted(world["hidden"])
    assert {t["id"] for t in clone_t.values()}.isdisjoint({t["id"] for t in src_t.values()})

    tools = _tools(test_client, world, clone["id"])
    assert tools[world["denied_tool"]]["policy"] == "deny"


def test_the_source_is_unchanged_and_the_copies_are_independent(test_client, world, update_tables_status_delta):
    before = _tables(test_client, world, world["agent"]["id"])
    clone = _clone(test_client, world).json()["data_source"]
    assert _tables(test_client, world, world["agent"]["id"]) == before

    # Changing the clone's selection leaves the source's alone.
    clone_t = _tables(test_client, world, clone["id"])
    update_tables_status_delta(data_source_id=clone["id"], deactivate=[clone_t[world["picked"][1]]["id"]],
                               user_token=world["admin"]["token"], org_id=world["org_id"])
    assert _tables(test_client, world, world["agent"]["id"])[world["picked"][1]]["is_active"] is True


def test_cloning_needs_manage_on_the_source_and_the_right_to_build_on_its_connections(test_client, world):
    assert _clone(test_client, world, actor="viewer").status_code == 403
    # Manages the agent, but may not build agents on its connections.
    assert _clone(test_client, world, actor="manager").status_code == 403
    assert _clone(test_client, world, actor="admin").status_code == 201


def test_a_taken_name_is_a_conflict_and_creates_nothing(test_client, world):
    before = _agent_ids(test_client, world)
    r = _clone(test_client, world, {"name": world["agent"]["name"]})
    assert r.status_code == 409
    assert _agent_ids(test_client, world) == before


def test_the_license_agent_cap_applies_to_a_clone(test_client, world):
    """Same cap as creating an agent (DataSourceService.enforce_agent_cap)."""
    from tests.e2e.test_license_limits import _license

    h = _h(world["admin"]["token"], world["org_id"])
    with _license(max_agents=-1):
        current = test_client.get("/api/license/usage", headers=h).json()["current_agents"]
    before = _agent_ids(test_client, world)
    with _license(max_agents=current):
        r = _clone(test_client, world)
    assert r.status_code == 402, r.text
    assert _agent_ids(test_client, world) == before
    with _license(max_agents=current + 1):
        assert _clone(test_client, world).status_code == 201


def test_a_connector_the_license_doesnt_cover_blocks_the_clone(test_client, world, monkeypatch):
    """Same connector check as creating an agent (enforce_connector_license)."""
    import app.ee.license as license_mod

    before = _agent_ids(test_client, world)
    monkeypatch.setattr(license_mod, "is_datasource_allowed", lambda t: t != "sqlite")
    r = _clone(test_client, world)
    assert r.status_code == 402 and "sqlite" in r.text
    assert _agent_ids(test_client, world) == before


def test_linking_saved_queries_is_refused_until_per_agent_runs_exist(test_client, world):
    assert _clone(test_client, world, {"saved_queries": "link"}).status_code == 422


@pytest.mark.parametrize("access,viewer_sees", [("copy", True), ("default", False)])
def test_access_copies_the_source_members_or_starts_with_the_cloner_only(test_client, world, access, viewer_sees):
    clone = _clone(test_client, world, {"access": access}).json()["data_source"]
    # Live right away (Training): exactly the people it was given to see it.
    assert (clone["id"] in _agent_ids(test_client, world, actor="viewer")) is viewer_sees
    assert clone["id"] in _agent_ids(test_client, world, actor="admin")
    # The source's manager manages the clone only when access was copied.
    r = test_client.put(f"/api/data_sources/{clone['id']}", json={"description": "x"},
                        headers=_h(world["manager"]["token"], world["org_id"]))
    assert (r.status_code == 200) is (access == "copy")


@pytest.mark.parametrize("files,shared", [("link", True), ("none", False)])
def test_agent_files_are_shared_or_left_out(test_client, world, files, shared):
    h = _h(world["admin"]["token"], world["org_id"])
    up = test_client.post(f"/api/data_sources/{world['agent']['id']}/files", headers=h,
                          files={"file": ("pricing.csv", io.BytesIO(b"plan,price\nbasic,10\n"), "text/csv")})
    assert up.status_code == 200, up.text
    clone = _clone(test_client, world, {"files": files}).json()
    listed = test_client.get(f"/api/data_sources/{clone['data_source']['id']}/files", headers=h)
    assert listed.status_code == 200, listed.text
    assert (up.json()["id"] in {f["id"] for f in listed.json()}) is shared
    assert clone["summary"]["files"]["linked"] == (1 if shared else 0)


def test_agent_prompts_are_copied_onto_the_clone(test_client, world, sqlite_data_source):
    h = _h(world["admin"]["token"], world["org_id"])
    # Also on another agent: the copy must not show up there a second time.
    other = sqlite_data_source(name=f"Ops {uuid.uuid4().hex[:4]}", user_token=world["admin"]["token"],
                               org_id=world["org_id"])
    r = test_client.post("/api/prompts", json={
        "title": "Weekly", "text": "Weekly revenue by region", "scope": "agent", "is_starter": True,
        "data_source_ids": [world["agent"]["id"], other["id"]],
    }, headers=h)
    assert r.status_code == 200, r.text
    source_prompt = r.json()
    clone = _clone(test_client, world).json()["data_source"]

    def _prompts(ds_id):
        res = test_client.get("/api/prompts", params={"data_source_id": ds_id}, headers=h)
        assert res.status_code == 200, res.text
        return res.json()["prompts"]

    on_clone = [p for p in _prompts(clone["id"]) if p["text"] == "Weekly revenue by region"]
    assert len(on_clone) == 1 and on_clone[0]["id"] != source_prompt["id"]
    assert on_clone[0]["data_source_ids"] == [clone["id"]] and on_clone[0]["is_starter"] is True
    # The source keeps its own prompt, and the other agent has it once.
    assert source_prompt["id"] in {p["id"] for p in _prompts(world["agent"]["id"])}
    assert [p["id"] for p in _prompts(other["id"]) if p["text"] == "Weekly revenue by region"] == [source_prompt["id"]]


def test_prompt_mentions_as_the_editor_saves_them_point_at_the_clone(test_client, world):
    """The prompt editor stores mentions grouped by display name, items with
    no "type" (MentionInput.buildMentionGroups); running a prompt picks its
    agent from the DATA SOURCES group, so that group must name the clone."""
    h = _h(world["admin"]["token"], world["org_id"])
    agent = world["agent"]
    table = _tables(test_client, world, agent["id"])[world["target"]]
    mentions = [
        {"name": "DATA SOURCES", "items": [{"id": agent["id"], "name": agent["name"]}]},
        {"name": "TABLES", "items": [{"id": table["id"], "name": table["name"],
                                      "datasource_id": agent["id"], "data_source_name": agent["name"]}]},
    ]
    r = test_client.post("/api/prompts", json={
        "title": "Weekly", "text": f'@"{agent["name"]}" weekly revenue', "scope": "agent",
        "data_source_ids": [agent["id"]], "mentions": mentions,
    }, headers=h)
    assert r.status_code == 200, r.text
    clone = _clone(test_client, world).json()["data_source"]
    clone_table = _tables(test_client, world, clone["id"])[world["target"]]

    res = test_client.get("/api/prompts", params={"data_source_id": clone["id"]}, headers=h)
    [copy] = [p for p in res.json()["prompts"] if p["title"] == "Weekly"]
    assert copy["text"] == f'@"{clone["name"]}" weekly revenue'
    assert copy["mentions"] == [
        {"name": "DATA SOURCES", "items": [{"id": clone["id"], "name": clone["name"]}]},
        {"name": "TABLES", "items": [{"id": clone_table["id"], "name": table["name"],
                                      "datasource_id": clone["id"], "data_source_name": clone["name"]}]},
    ]


def test_private_and_global_prompts_that_name_the_source_stay_behind(test_client, world):
    """Only the agent's own prompts come along: a user's private prompt or an
    org-wide one that names the source would show up twice."""
    admin_h = _h(world["admin"]["token"], world["org_id"])
    viewer_h = _h(world["viewer"]["token"], world["org_id"])
    agent_id = world["agent"]["id"]
    for scope, h in (("private", viewer_h), ("global", admin_h), ("agent", admin_h)):
        r = test_client.post("/api/prompts", json={
            "title": f"{scope} prompt", "text": f"{scope} revenue", "scope": scope, "data_source_ids": [agent_id],
        }, headers=h)
        assert r.status_code == 200, r.text

    body = _clone(test_client, world, {"access": "copy"}).json()
    assert body["summary"]["prompts"]["copied"] == 1

    def _count(h, text):
        res = test_client.get("/api/prompts", headers=h)
        assert res.status_code == 200, res.text
        return sum(p["text"] == text for p in res.json()["prompts"])
    assert _count(viewer_h, "private revenue") == 1
    assert _count(admin_h, "global revenue") == 1
    assert _count(admin_h, "agent revenue") == 2


@pytest.mark.parametrize("status", ["published", "draft", "disabled"])
def test_the_clone_is_never_more_visible_than_its_source(test_client, world, status):
    h = _h(world["admin"]["token"], world["org_id"])
    r = test_client.put(f"/api/data_sources/{world['agent']['id']}", json={"publish_status": status}, headers=h)
    assert r.status_code == 200, r.text
    clone = _clone(test_client, world, {"access": "copy"}).json()["data_source"]
    assert clone["publish_status"] == status and clone["reliability_status"] == "training"


def test_the_clone_name_does_not_shadow_a_mention_of_the_source(test_client, world):
    """Named "<source> EU", the clone is a longer name that @<source> EU would
    match first; the text was written before it existed, so @<source> wins."""
    h = _h(world["admin"]["token"], world["org_id"])
    name = world["agent"]["name"]
    r = test_client.post("/api/prompts", json={
        "title": "EU", "text": f"@{name} EU revenue", "scope": "agent", "data_source_ids": [world["agent"]["id"]],
    }, headers=h)
    assert r.status_code == 200, r.text
    clone = _clone(test_client, world, {"name": f"{name} EU"}).json()["data_source"]
    res = test_client.get("/api/prompts", params={"data_source_id": clone["id"]}, headers=h)
    [copy] = [p for p in res.json()["prompts"] if p["title"] == "EU"]
    assert copy["text"] == f'@"{name} EU" EU revenue'


def test_a_clone_that_fails_part_way_leaves_nothing_behind(test_client, world, monkeypatch):
    """Fail at the last write before the commit (the audit entry): every row
    written before it must be gone too."""
    from app.ee.audit.service import audit_service

    before = _agent_ids(test_client, world)

    async def _boom(*a, **kw):
        if kw.get("action") == "data_source.cloned":
            raise RuntimeError("injected")
    monkeypatch.setattr(audit_service, "log", _boom)
    name = f"Doomed {uuid.uuid4().hex[:6]}"
    try:
        r = _clone(test_client, world, {"name": name, "access": "copy"})
        assert r.status_code >= 500
    except RuntimeError:
        pass  # the test client re-raises server errors
    monkeypatch.undo()

    assert _agent_ids(test_client, world) == before
    # The name is free again: no half-built agent holds it.
    assert _clone(test_client, world, {"name": name}).status_code == 201


def test_empty_json_settings_are_sql_null_on_the_clone(test_client, world):
    """An empty JSON setting on the clone is SQL NULL, never the JSON text
    'null' (which ``IS NULL`` doesn't match), whichever form the source holds:
    agents made by POST /data_sources hold 'null', demo installs hold NULL.
    Read from the database: the API returns both as null."""
    import asyncio

    from sqlalchemy import text

    from app.dependencies import async_session_maker

    clone = _clone(test_client, world).json()["data_source"]

    async def _forms():
        async with async_session_maker() as db:
            agent = (await db.execute(text(
                "select channel_availability is null, automation_settings is null "
                "from data_sources where id = :c"), {"c": clone["id"]})).one()
            json_text_nulls = (await db.execute(text(
                "select count(*) from datasource_tables where datasource_id = :c "
                "and cast(excluded_columns as text) = 'null'"),
                {"c": clone["id"]})).scalar()
            src_empty = (await db.execute(text(
                "select count(*) from datasource_tables where datasource_id = :a and deleted_at is null "
                "and (excluded_columns is null or cast(excluded_columns as text) = 'null')"),
                {"a": world["agent"]["id"]})).scalar()
            clone_null = (await db.execute(text(
                "select count(*) from datasource_tables where datasource_id = :c and excluded_columns is null"),
                {"c": clone["id"]})).scalar()
            return tuple(agent), json_text_nulls, src_empty, clone_null
    agent, json_text_nulls, src_empty, clone_null = asyncio.run(_forms())
    assert tuple(bool(x) for x in agent) == (True, True)
    assert json_text_nulls == 0
    assert clone_null == src_empty  # every table without hidden columns is NULL on the clone
