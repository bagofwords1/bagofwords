"""World projection: real APIs and real access rules, no external connectors."""

import uuid
from pathlib import Path

import pytest


def headers(token, org):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org}


@pytest.fixture
def world_cast(test_client, create_user, login_user, whoami, invite_user_to_org):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    member = invite_user_to_org(org_id=org, admin_token=token)
    other = invite_user_to_org(org_id=org, admin_token=token)
    assert test_client.get("/api/world", headers=headers(token, org)).status_code == 404
    enabled = {"config": {"general": {"show_world": True}}}
    assert (
        test_client.put("/api/organization/settings", json=enabled, headers=headers(member["token"], org)).status_code
        == 403
    )
    assert test_client.put("/api/organization/settings", json=enabled, headers=headers(token, org)).status_code == 200
    return org, token, member["token"], other["token"]


def graph(client, token, org, **params):
    response = client.get("/api/world", params=params, headers=headers(token, org))
    assert response.status_code == 200, response.text
    body = response.json()
    ids = {node["id"] for node in body["nodes"]}
    assert len(ids) == len(body["nodes"])
    assert all(edge["source"] in ids and edge["target"] in ids for edge in body["edges"])
    return body


def test_world_memories_are_private_even_from_admin_and_disappear_when_forgotten(test_client, world_cast):
    org, admin, member, other = world_cast
    made = test_client.post(
        "/api/users/me/memory", json={"text": "Leads the regional expansion project"}, headers=headers(member, org)
    )
    assert made.status_code == 200, made.text
    id = made.json()["id"]
    assert any(n["resource_id"] == id for n in graph(test_client, member, org, section="memory")["nodes"])
    for token in (admin, other):
        assert not any(n["type"] == "memory" for n in graph(test_client, token, org, section="memory")["nodes"])
    assert test_client.delete(f"/api/users/me/memory/{id}", headers=headers(member, org)).status_code == 200
    assert not any(n["type"] == "memory" for n in graph(test_client, member, org, section="memory")["nodes"])


def test_world_memory_respects_disabled_setting(test_client, world_cast):
    org, admin, member, _ = world_cast
    assert (
        test_client.post(
            "/api/users/me/memory", json={"text": "Owns the monthly board presentation"}, headers=headers(member, org)
        ).status_code
        == 200
    )
    r = test_client.put(
        "/api/organization/settings",
        json={"config": {"enable_user_memory": {"value": False}}},
        headers=headers(admin, org),
    )
    assert r.status_code == 200, r.text
    assert not any(n["type"] == "memory" for n in graph(test_client, member, org, section="memory")["nodes"])


def test_world_never_expands_an_inaccessible_agent(test_client, world_cast, create_data_source):
    org, admin, member, _ = world_cast
    ds = create_data_source(
        name=f"Private-{uuid.uuid4().hex[:8]}",
        type="sqlite",
        config={"database": str(Path(__file__).resolve().parents[2] / "config/chinook.sqlite")},
        user_token=admin,
        org_id=org,
    )
    r = test_client.put(f"/api/data_sources/{ds['id']}", json={"is_public": False}, headers=headers(admin, org))
    assert r.status_code == 200, r.text
    own = graph(test_client, admin, org)
    assert any(n["resource_id"] == ds["id"] for n in own["nodes"])
    hidden = graph(test_client, member, org)
    assert ds["name"] not in str(hidden)
    denied = test_client.get(
        "/api/world", params={"section": "agent", "agent_id": ds["id"]}, headers=headers(member, org)
    )
    assert denied.status_code == 404
    visible = graph(test_client, admin, org, section="agent", agent_id=ds["id"])
    assert any(n["type"] == "agent" for n in visible["nodes"])


def test_world_private_reports_cannot_be_listed_or_expanded_by_other_members(test_client, world_cast, create_report):
    org, _, owner, other = world_cast
    report = create_report(title=f"Private-{uuid.uuid4().hex[:8]}", user_token=owner, org_id=org)
    query = test_client.post(
        "/api/queries",
        json={
            "title": "Private revenue investigation",
            "description": "Compare the regional drivers",
            "report_id": report["id"],
        },
        headers=headers(owner, org),
    )
    assert query.status_code == 200, query.text
    assert any(n["resource_id"] == report["id"] for n in graph(test_client, owner, org, section="reports")["nodes"])
    assert report["title"] not in str(graph(test_client, other, org, section="reports"))
    response = test_client.get(
        "/api/world", params={"section": "report", "report_id": report["id"]}, headers=headers(other, org)
    )
    assert response.status_code == 404
    expanded = graph(test_client, owner, org, section="report", report_id=report["id"])
    assert any(n["type"] == "query" and n["resource_id"] == query.json()["id"] for n in expanded["nodes"])
    assert any(
        e["source"] == f"report:{report['id']}" and e["target"] == f"query:{query.json()['id']}"
        for e in expanded["edges"]
    )


def test_world_file_listing_uses_file_access_not_organization_membership(test_client, world_cast):
    org, admin, owner, other = world_cast
    name = f"private-{uuid.uuid4().hex[:8]}.csv"
    made = test_client.post(
        "/api/files", files={"file": (name, b"region,total\neast,12\n", "text/csv")}, headers=headers(owner, org)
    )
    assert made.status_code == 200, made.text
    for token in (owner, admin):
        assert name in str(graph(test_client, token, org, section="files"))
    assert name not in str(graph(test_client, other, org, section="files"))


def test_world_rejects_cross_organization_requests(test_client, world_cast):
    _, _, member, _ = world_cast
    response = test_client.get("/api/world", headers=headers(member, str(uuid.uuid4())))
    assert response.status_code in (403, 404)


def test_world_includes_published_instructions(test_client, world_cast, create_instruction):
    org, admin, _, _ = world_cast
    instruction = create_instruction(
        text="Recognize annual contracts monthly, excluding test accounts.",
        user_token=admin,
        org_id=org,
        status="published",
    )
    result = graph(test_client, admin, org, section="instructions")
    assert any(n["resource_id"] == instruction["id"] for n in result["nodes"])


def test_world_tool_layers_respect_user_preferences_and_admin_denial(
    test_client,
    create_user,
    login_user,
    whoami,
    enable_mcp,
    create_mcp_connection,
    create_domain_from_connection,
    invite_user_to_org,
):
    from tests.e2e.rbac.test_rbac_tool_policies import _get_tools, _setup_agent_with_tools

    ctx = _setup_agent_with_tools(
        create_user=create_user,
        login_user=login_user,
        whoami=whoami,
        enable_mcp=enable_mcp,
        create_mcp_connection=create_mcp_connection,
        create_domain_from_connection=create_domain_from_connection,
        test_client=test_client,
    )
    org, admin, aid = ctx["org_id"], ctx["admin_token"], ctx["ds_id"]
    member = invite_user_to_org(org_id=org, admin_token=admin)["token"]
    assert test_client.put(
        "/api/organization/settings", headers=headers(admin, org), json={"config": {"general": {"show_world": True}}}
    ).is_success
    tool = _get_tools(test_client, aid, admin, org)["echo"]

    def shown(token):
        return {
            n["resource_id"]
            for n in graph(test_client, token, org, section="agent", agent_id=aid)["nodes"]
            if n["type"] == "tool"
        }

    assert tool["id"] in shown(member)
    r = test_client.put(
        f"/api/data_sources/{aid}/tools/{tool['id']}/my_policy", json={"policy": "deny"}, headers=headers(member, org)
    )
    assert r.status_code == 200, r.text
    assert tool["id"] not in shown(member)
    assert tool["id"] in shown(admin)
    r = test_client.put(
        f"/api/data_sources/{aid}/tools/{tool['id']}", json={"policy": "deny"}, headers=headers(admin, org)
    )
    assert r.status_code == 200, r.text
    assert tool["id"] not in shown(admin)


def test_folder_hierarchy_respects_direct_grants_and_world_toggle(test_client, world_cast, whoami):
    org, admin, member, _ = world_cast
    auth = headers(admin, org)
    parent = test_client.post("/api/folders", headers=auth, json={"name": "Marketing"}).json()["id"]
    child = test_client.post("/api/folders", headers=auth, json={"name": "Brand", "parent_id": parent}).json()["id"]
    assert any(
        e["source"] == f"folder:{parent}" and e["target"] == f"folder:{child}"
        for e in graph(test_client, admin, org)["edges"]
    )
    assert parent not in str(graph(test_client, member, org))
    assert (
        test_client.get(
            "/api/world", params={"section": "folder", "folder_id": child}, headers=headers(member, org)
        ).status_code
        == 404
    )

    def knowledge(folder, kind, **body):
        response = test_client.post(f"/api/folders/{folder}/knowledge/{kind}", headers=auth, json=body)
        assert response.is_success, response.text
        return response.json()["id"]

    rule = knowledge(parent, "instructions", title="Routing", text="Choose Brand")
    assert test_client.put(f"/api/folders/{child}/knowledge/instructions/{rule}", headers=auth).is_success
    suite = knowledge(child, "evals", name="Brand routing checks")
    for folder in (parent, child):
        edges = graph(test_client, admin, org, section="folder", folder_id=folder)["edges"]
        assert any(e["source"] == f"folder:{folder}" and e["target"] == f"instruction:{rule}" for e in edges)
    assert any(e["target"] == f"eval:{suite}" for e in edges)
    assert test_client.put(
        f"/api/folders/{child}/share",
        headers=auth,
        json={"principal_type": "user", "principal_id": whoami(member)["id"], "role": "Viewer"},
    ).is_success
    child_graph = graph(test_client, member, org, section="folder", folder_id=child)
    assert child in str(child_graph) and parent not in str(child_graph) and suite not in str(child_graph)
    assert (
        test_client.put(
            "/api/organization/settings", headers=auth, json={"config": {"general": {"show_world": False}}}
        ).status_code
        == 200
    )
    assert (
        test_client.get("/api/world", params={"section": "folder", "folder_id": child}, headers=auth).status_code == 404
    )


def test_world_overview_layers_load_for_members(test_client, world_cast):
    org, admin, member, _ = world_cast
    for token in (admin, member):
        for section in ("overview", "tasks", "reports", "projects", "artifacts", "instructions", "files", "evals"):
            graph(test_client, token, org, section=section)


def test_world_projects_300_agents_without_loading_their_internals(test_client, world_cast, whoami):
    import asyncio

    from app.dependencies import async_session_maker
    from app.models.data_source import DataSource
    from app.models.folder import Folder

    org, admin, member, _ = world_cast
    owner = whoami(admin)["id"]

    async def seed():
        async with async_session_maker() as db:
            folders = [Folder(name=f"Team {i}", creator_id=owner, organization_id=org) for i in range(15)]
            db.add_all(folders)
            await db.flush()
            db.add_all(
                [
                    DataSource(
                        name=f"Catalog agent {i}",
                        folder_id=folders[i % 15].id,
                        organization_id=org,
                        owner_user_id=owner,
                        is_public=False,
                    )
                    for i in range(300)
                ]
            )
            await db.commit()

    asyncio.run(seed())
    result = graph(test_client, admin, org)
    assert sum(n["type"] == "agent" for n in result["nodes"]) == 300
    assert sum(e["relation"] == "contains" for e in result["edges"]) == 300
    assert not any(n["type"] in {"table", "tool"} for n in result["nodes"])
    assert not graph(test_client, member, org)["nodes"]
