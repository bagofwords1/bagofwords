"""
E2E tests for agent catalogs (org-level, organizational-only groupings of agents).

Covers:
- POST/GET/PUT/DELETE /agent_catalogs (CRUD, name validation, duplicate names)
- PUT /agent_catalogs/{id}/agents: set membership, move between catalogs,
  clear, unknown agent ids
- catalog_id surfaces on agent list items; deleting a catalog uncatalogues
  its agents (the agents themselves stay)
- Writes are full-admin only; any member may list
"""
import uuid

import pytest


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _admin(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return token, org_id


def _create_agent(test_client, token, org_id, root, name):
    response = test_client.post(
        "/api/data_sources",
        json={
            "name": name,
            "type": "network_dir",
            "config": {"root_path": str(root)},
            "credentials": {"auth_type": "none"},
            "auth_policy": "system_only",
            "generate_summary": False,
            "generate_conversation_starters": False,
            "generate_ai_rules": False,
        },
        headers=_headers(token, org_id),
    )
    assert response.status_code == 200, response.json()
    return response.json()["id"]


def _catalog_ids_by_agent(test_client, token, org_id):
    response = test_client.get(
        "/api/data_sources/active",
        params={"show_all": True, "include_unconnected": True},
        headers=_headers(token, org_id),
    )
    assert response.status_code == 200, response.json()
    return {d["id"]: d.get("catalog_id") for d in response.json()}


@pytest.mark.e2e
def test_agent_catalog_crud_and_membership(test_client, create_user, login_user, whoami, tmp_path):
    token, org_id = _admin(create_user, login_user, whoami)
    h = _headers(token, org_id)
    a1 = _create_agent(test_client, token, org_id, tmp_path, "Budget")
    a2 = _create_agent(test_client, token, org_id, tmp_path, "Cash Flow")
    a3 = _create_agent(test_client, token, org_id, tmp_path, "Churn")

    # Create
    finance = test_client.post("/api/agent_catalogs", json={"name": "  Finance ", "color": "#16a34a"}, headers=h)
    assert finance.status_code == 200, finance.json()
    finance = finance.json()
    assert finance["name"] == "Finance"
    assert finance["agent_count"] == 0
    sales = test_client.post("/api/agent_catalogs", json={"name": "Sales"}, headers=h).json()

    # Name validation
    assert test_client.post("/api/agent_catalogs", json={"name": "   "}, headers=h).status_code == 422
    assert test_client.post("/api/agent_catalogs", json={"name": "finance"}, headers=h).status_code == 409
    assert test_client.put(f"/api/agent_catalogs/{sales['id']}", json={"name": "FINANCE"}, headers=h).status_code == 409

    # Set membership
    r = test_client.put(f"/api/agent_catalogs/{finance['id']}/agents", json={"data_source_ids": [a1, a2]}, headers=h)
    assert r.status_code == 200, r.json()
    assert r.json()["agent_count"] == 2
    by_agent = _catalog_ids_by_agent(test_client, token, org_id)
    assert by_agent[a1] == finance["id"] and by_agent[a2] == finance["id"] and by_agent[a3] is None

    # Moving an agent: one catalog per agent, and the omitted agent is uncatalogued
    test_client.put(f"/api/agent_catalogs/{sales['id']}/agents", json={"data_source_ids": [a2, a3]}, headers=h)
    test_client.put(f"/api/agent_catalogs/{finance['id']}/agents", json={"data_source_ids": []}, headers=h)
    by_agent = _catalog_ids_by_agent(test_client, token, org_id)
    assert by_agent[a1] is None
    assert by_agent[a2] == sales["id"] and by_agent[a3] == sales["id"]

    listed = {c["id"]: c for c in test_client.get("/api/agent_catalogs", headers=h).json()}
    assert listed[finance["id"]]["agent_count"] == 0
    assert listed[sales["id"]]["agent_count"] == 2
    assert [c["name"] for c in listed.values()] == ["Finance", "Sales"]

    # Rename
    renamed = test_client.put(f"/api/agent_catalogs/{sales['id']}", json={"name": "Revenue", "description": "Sales & revenue"}, headers=h)
    assert renamed.status_code == 200, renamed.json()
    assert renamed.json()["name"] == "Revenue"
    assert renamed.json()["agent_count"] == 2

    # Unknown agent id
    bad = test_client.put(f"/api/agent_catalogs/{sales['id']}/agents", json={"data_source_ids": [a2, str(uuid.uuid4())]}, headers=h)
    assert bad.status_code == 404
    assert _catalog_ids_by_agent(test_client, token, org_id)[a2] == sales["id"]

    # Delete uncatalogues the agents but keeps them
    assert test_client.delete(f"/api/agent_catalogs/{sales['id']}", headers=h).status_code == 200
    by_agent = _catalog_ids_by_agent(test_client, token, org_id)
    assert {a1, a2, a3} <= set(by_agent)
    assert by_agent[a2] is None and by_agent[a3] is None
    assert sales["id"] not in {c["id"] for c in test_client.get("/api/agent_catalogs", headers=h).json()}
    assert test_client.put(f"/api/agent_catalogs/{sales['id']}", json={"name": "X"}, headers=h).status_code == 404

    # Single-agent assignment (agent page): set, move, clear, bad ids
    one = test_client.put(f"/api/data_sources/{a1}/catalog", json={"catalog_id": finance["id"]}, headers=h)
    assert one.status_code == 200, one.json()
    assert one.json() == {"catalog_id": finance["id"]}
    single = test_client.get(f"/api/data_sources/{a1}", headers=h).json()
    assert single["catalog_id"] == finance["id"]
    assert test_client.put(f"/api/data_sources/{a1}/catalog", json={"catalog_id": None}, headers=h).status_code == 200
    assert _catalog_ids_by_agent(test_client, token, org_id)[a1] is None
    assert test_client.put(f"/api/data_sources/{a1}/catalog", json={"catalog_id": sales["id"]}, headers=h).status_code == 404
    assert test_client.put(f"/api/data_sources/{uuid.uuid4()}/catalog", json={"catalog_id": finance["id"]}, headers=h).status_code == 404

    # A deleted catalog's name can be reused
    assert test_client.post("/api/agent_catalogs", json={"name": "Revenue"}, headers=h).status_code == 200


@pytest.mark.e2e
def test_agent_catalog_writes_are_admin_only(test_client, create_user, login_user, whoami, tmp_path):
    token, org_id = _admin(create_user, login_user, whoami)
    agent = _create_agent(test_client, token, org_id, tmp_path, "Budget")
    catalog = test_client.post("/api/agent_catalogs", json={"name": "Finance"}, headers=_headers(token, org_id)).json()

    member_email = f"catalog_member_{uuid.uuid4().hex[:6]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers=_headers(token, org_id),
    )
    create_user(email=member_email, password="test123")
    member = _headers(login_user(email=member_email, password="test123"), org_id)

    listed = test_client.get("/api/agent_catalogs", headers=member)
    assert listed.status_code == 200
    assert [c["id"] for c in listed.json()] == [catalog["id"]]

    assert test_client.post("/api/agent_catalogs", json={"name": "Mine"}, headers=member).status_code == 403
    assert test_client.put(f"/api/agent_catalogs/{catalog['id']}", json={"name": "Mine"}, headers=member).status_code == 403
    assert test_client.put(f"/api/agent_catalogs/{catalog['id']}/agents", json={"data_source_ids": [agent]}, headers=member).status_code == 403
    assert test_client.delete(f"/api/agent_catalogs/{catalog['id']}", headers=member).status_code == 403
    assert test_client.put(f"/api/data_sources/{agent}/catalog", json={"catalog_id": catalog["id"]}, headers=member).status_code == 403
