"""Deleting an agent that a project lists among its default agents.

A project keeps its default agents in `project_data_source_association`, a
many-to-many declared only on the Project side, so the ORM does not know it
points at the agent. The agent delete must detach it — otherwise Postgres
rejects the final DELETE on that foreign key AFTER the delete has already
removed the agent's own instructions and saved queries (SQLite, which the
suite runs on by default, does not enforce the key, so this asserts the rows
directly).
"""
import uuid

import pytest
from sqlalchemy import select


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _project_agent_ids(project_id):
    """Read the association table itself: the API only lists agents that
    still exist, so a dangling row is invisible through it."""
    import asyncio
    from app.dependencies import async_session_maker
    from app.models.project import project_data_source_association as assoc

    async def read():
        async with async_session_maker() as db:
            rows = await db.execute(select(assoc.c.data_source_id).where(assoc.c.project_id == project_id))
            return {str(r[0]) for r in rows.all()}

    return asyncio.run(read())


@pytest.mark.e2e
def test_deleting_an_agent_detaches_it_from_projects_and_keeps_the_project(
    test_client, bootstrap_admin, sqlite_data_source,
):
    admin = bootstrap_admin("admin")
    h = _auth(admin["token"], admin["org_id"])
    doomed = sqlite_data_source(name=f"doomed_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=admin["org_id"])
    kept = sqlite_data_source(name=f"kept_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=admin["org_id"])

    project = test_client.post("/api/projects", json={"name": f"P {uuid.uuid4().hex[:4]}"}, headers=h)
    assert project.status_code == 200, project.text
    pid = project.json()["id"]
    linked = test_client.put(
        f"/api/projects/{pid}/data_sources", json={"data_source_ids": [doomed["id"], kept["id"]]}, headers=h,
    )
    assert linked.status_code == 200, linked.text
    assert _project_agent_ids(pid) == {doomed["id"], kept["id"]}

    gone = test_client.delete(f"/api/data_sources/{doomed['id']}", headers=h)
    assert gone.status_code in (200, 204), gone.text

    assert _project_agent_ids(pid) == {kept["id"]}
    still_there = test_client.get(f"/api/projects/{pid}", headers=h)
    assert still_there.status_code == 200, still_there.text


@pytest.mark.e2e
def test_an_unhandled_reference_stops_the_delete_before_anything_is_deleted(
    test_client, bootstrap_admin, sqlite_data_source, monkeypatch,
):
    """If a reference the delete does not clear points at the agent, nothing
    — not even the agent's own instructions — may be deleted."""
    from app.services import data_source_service as dss

    admin = bootstrap_admin("admin")
    h = _auth(admin["token"], admin["org_id"])
    agent = sqlite_data_source(name=f"a_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=admin["org_id"])
    ins = test_client.post(
        "/api/instructions",
        json={"text": f"only here {uuid.uuid4().hex[:6]}", "status": "published", "category": "general",
              "load_mode": "always", "data_source_ids": [agent["id"]]},
        headers=h,
    )
    assert ins.status_code == 200, ins.text
    pid = test_client.post("/api/projects", json={"name": f"P {uuid.uuid4().hex[:4]}"}, headers=h).json()["id"]
    assert test_client.put(f"/api/projects/{pid}/data_sources", json={"data_source_ids": [agent["id"]]}, headers=h).status_code == 200

    # Pretend the delete did not know about projects (the state before this fix).
    monkeypatch.setattr(dss, "AGENT_DELETE_CLEARS", dss.AGENT_DELETE_CLEARS - {"project_data_source_association"})

    resp = test_client.delete(f"/api/data_sources/{agent['id']}", headers=h)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error_code"] == "data_source.in_use"

    assert test_client.get(f"/api/data_sources/{agent['id']}", headers=h).status_code == 200
    assert test_client.get(f"/api/instructions/{ins.json()['id']}", headers=h).status_code == 200


def _agent_connection_id(test_client, ds, h):
    resp = test_client.get(f"/api/data_sources/{ds['id']}", headers=h)
    assert resp.status_code == 200, resp.text
    conns = resp.json().get("connections") or []
    assert len(conns) == 1, conns
    return conns[0]["id"]


@pytest.mark.e2e
def test_deleting_a_connection_deletes_its_only_agent_like_an_agent_delete(
    test_client, bootstrap_admin, sqlite_data_source,
):
    """An agent that exists only through a connection goes with it through the
    same delete as deleting the agent: detached from projects, its own
    instructions removed — not a bare row delete that leaves references."""
    admin = bootstrap_admin("admin")
    h = _auth(admin["token"], admin["org_id"])
    agent = sqlite_data_source(name=f"c_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=admin["org_id"])
    kept = sqlite_data_source(name=f"k_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=admin["org_id"])
    ins = test_client.post(
        "/api/instructions",
        json={"text": f"only here {uuid.uuid4().hex[:6]}", "status": "published", "category": "general",
              "load_mode": "always", "data_source_ids": [agent["id"]]},
        headers=h,
    )
    assert ins.status_code == 200, ins.text
    pid = test_client.post("/api/projects", json={"name": f"P {uuid.uuid4().hex[:4]}"}, headers=h).json()["id"]
    assert test_client.put(
        f"/api/projects/{pid}/data_sources", json={"data_source_ids": [agent["id"], kept["id"]]}, headers=h,
    ).status_code == 200

    conn_id = _agent_connection_id(test_client, agent, h)
    gone = test_client.delete(f"/api/connections/{conn_id}", headers=h)
    assert gone.status_code == 200, gone.text
    assert agent["name"] in gone.json().get("deleted_agents", []) or gone.json().get("impacted_agents") == 1

    assert test_client.get(f"/api/data_sources/{agent['id']}", headers=h).status_code == 404
    assert _project_agent_ids(pid) == {kept["id"]}
    assert test_client.get(f"/api/instructions/{ins.json()['id']}", headers=h).status_code == 404
