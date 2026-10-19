"""A failed connection test turns an agent off only when its connection is shared.

``GET /data_sources/{id}/test_connection`` run by someone who manages the
agent records the outcome: a failed probe marks the agent inactive (hidden
from every agent list) and a passing one turns it back on. That is meant for
shared (``system_only``) connections, where one account serves everybody, so
a failure means the agent is broken for everybody.

On a per-user connection (``user_required``) each person signs in with their
own account. A failed probe says something about the caller only -- typically
that they have not signed in -- and must not take the agent away from the
whole organization. The agent-level check read ``auth_policy`` off the agent,
which no longer carries it (it moved to the connection), so it always saw the
default ``system_only`` and turned per-user agents off too.
"""
import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.data_source import DataSource
from tests.e2e.rbac.test_rbac_entity_creation import _run


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


async def _connection_ids(ds_id):
    from app.models.domain_connection import domain_connection
    async with async_session_maker() as db:
        return list((await db.execute(select(domain_connection.c.connection_id).where(
            domain_connection.c.data_source_id == str(ds_id)))).scalars().all())


async def _make_per_user(ds_id):
    # The API sets a connection's auth policy through the connection editor,
    # which also wants the per-user sign-in modes configured; only the policy
    # matters here, so it is set directly.
    async with async_session_maker() as db:
        for cid in await _connection_ids(ds_id):
            (await db.get(Connection, cid)).auth_policy = "user_required"
        await db.commit()


async def _agent_active(ds_id):
    async with async_session_maker() as db:
        return bool((await db.get(DataSource, ds_id)).is_active)


@pytest.fixture
def world(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    ds = sqlite_data_source(name="agent_x", user_token=admin["token"], org_id=org_id, is_public=True)
    manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    g = grant_resource(resource_type="data_source", resource_id=ds["id"],
                       principal_type="user", principal_id=manager["user_id"],
                       permissions=["manage"], user_token=admin["token"], org_id=org_id)
    assert g.status_code == 200, g.text
    return {"org_id": org_id, "admin": admin, "manager": manager, "ds": ds}


def _test(test_client, w, who):
    r = test_client.get(f"/api/data_sources/{w['ds']['id']}/test_connection",
                        headers=_hdr(w[who]["token"], w["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()


def _listed(test_client, w, who):
    r = test_client.get("/api/data_sources/active?include_unconnected=true",
                        headers=_hdr(w[who]["token"], w["org_id"]))
    assert r.status_code == 200, r.text
    return w["ds"]["id"] in {d["id"] for d in r.json()}


@pytest.mark.e2e
@pytest.mark.parametrize("who", ["admin", "manager"])
def test_a_failed_test_of_a_per_user_connection_keeps_the_agent(test_client, world, who, monkeypatch):
    from app.data_sources.clients.sqlite_client import SqliteClient

    w = world
    _run(_make_per_user(w["ds"]["id"]))
    # The caller's own probe fails (mocked at the driver: not signed in, an
    # expired token, a network hiccup -- all about this caller only).
    monkeypatch.setattr(SqliteClient, "test_connection",
                        lambda self: {"success": False, "message": "unreachable"})
    assert _test(test_client, w, who).get("success") is False
    assert _run(_agent_active(w["ds"]["id"])) is True, "a per-user probe turned the agent off for everyone"
    assert _listed(test_client, w, "admin")


@pytest.mark.e2e
def test_a_failed_test_of_a_shared_connection_still_turns_the_agent_off(test_client, world, monkeypatch):
    from app.data_sources.clients.sqlite_client import SqliteClient

    w = world
    # The shared account's database stops answering (mocked at the driver).
    monkeypatch.setattr(SqliteClient, "test_connection",
                        lambda self: {"success": False, "message": "unreachable"})
    assert _test(test_client, w, "admin").get("success") is False
    assert _run(_agent_active(w["ds"]["id"])) is False
    monkeypatch.undo()
    # And a passing test turns it back on.
    assert _test(test_client, w, "admin").get("success") is True
    assert _run(_agent_active(w["ds"]["id"])) is True
