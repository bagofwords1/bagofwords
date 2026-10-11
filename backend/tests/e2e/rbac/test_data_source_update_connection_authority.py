"""
PUT /api/data_sources/{id} must not be a side door around connection RBAC.

`config`, `credentials` and `auth_policy` on the data-source update are
delegated to the agent's underlying Connection (shared by every agent linked
to it). Rewriting them therefore needs `manage_connection` on that connection
— the same authority PUT /api/connections/{id} requires — not merely `manage`
on the data source. Everything else a data-source manager does (rename,
describe, unlink/relink connections they may build on) keeps working.

World:
    admin       — full_admin; creates a SQL connection + an MCP connection and
                  one agent linked to both
    ds_manager  — `manage` on the agent, `create_data_sources` on both
                  connections (may build on them), NO `manage_connection`
    conn_admin  — `manage` on the agent + `manage_connection` on both connections
"""
import pytest
from pathlib import Path

from tests.fixtures.connection_tool import _skip_network

CHINOOK = (Path(__file__).resolve().parents[2] / "config" / "chinook.sqlite").resolve()


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def world(
    test_client, bootstrap_admin, invite_user_to_org,
    create_connection, create_mcp_connection, grant_resource,
):
    if not CHINOOK.exists():
        pytest.skip(f"Missing SQLite fixture at {CHINOOK}")

    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    ah = _hdr(admin["token"], org_id)

    sql_conn = create_connection(
        name="shared-sql", type="sqlite", config={"database": str(CHINOOK)},
        credentials={}, user_token=admin["token"], org_id=org_id,
    )
    mcp_conn = create_mcp_connection(
        name="shared-mcp", server_url="http://mock-mcp:3000/mcp",
        user_token=admin["token"], org_id=org_id,
    )

    with _skip_network():
        r = test_client.post("/api/data_sources", headers=ah, json={
            "name": "two-conn-agent",
            "connection_ids": [sql_conn["id"], mcp_conn["id"]],
            "generate_summary": False,
            "generate_conversation_starters": False,
            "generate_ai_rules": False,
        })
    assert r.status_code == 200, r.text
    ds_id = r.json()["id"]

    def _member(conn_perms):
        m = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
        g = grant_resource(resource_type="data_source", resource_id=ds_id,
                           principal_type="user", principal_id=m["user_id"],
                           permissions=["manage"], user_token=admin["token"], org_id=org_id)
        assert g.status_code == 200, g.json()
        for cid in (sql_conn["id"], mcp_conn["id"]):
            g = grant_resource(resource_type="connection", resource_id=cid,
                               principal_type="user", principal_id=m["user_id"],
                               permissions=conn_perms, user_token=admin["token"], org_id=org_id)
            assert g.status_code == 200, g.json()
        return m

    return {
        "org_id": org_id,
        "admin": admin,
        "ds_id": ds_id,
        "sql_conn": sql_conn,
        "mcp_conn": mcp_conn,
        "ds_manager": _member(["create_data_sources"]),
        "conn_admin": _member(["manage_connection"]),
    }


def _conn_state(test_client, w, conn_id):
    r = test_client.get(f"/api/connections/{conn_id}", headers=_hdr(w["admin"]["token"], w["org_id"]))
    assert r.status_code == 200, r.text
    body = r.json()
    return body.get("config"), body.get("auth_policy")


def _ds_conn_ids(test_client, w):
    r = test_client.get(f"/api/data_sources/{w['ds_id']}", headers=_hdr(w["admin"]["token"], w["org_id"]))
    assert r.status_code == 200, r.text
    return {str(c["id"]) for c in r.json().get("connections") or []}


@pytest.mark.e2e
@pytest.mark.parametrize("payload", [
    {"config": {"database": "/tmp/attacker.sqlite"}},
    {"credentials": {"password": "attacker"}},
    {"auth_policy": "user_required"},
    {"name": "harmless-rename", "config": {"database": "/tmp/attacker.sqlite"}},
])
def test_ds_manager_without_connection_manage_cannot_rewrite_connection(test_client, world, payload):
    w = world
    hdr = _hdr(w["ds_manager"]["token"], w["org_id"])
    before = {cid: _conn_state(test_client, w, cid) for cid in (w["sql_conn"]["id"], w["mcp_conn"]["id"])}
    ds_before = test_client.get(f"/api/data_sources/{w['ds_id']}", headers=hdr).json()

    r = test_client.put(f"/api/data_sources/{w['ds_id']}", json=payload, headers=hdr)
    assert r.status_code == 403, r.text

    for cid, state in before.items():
        assert _conn_state(test_client, w, cid) == state
    # The whole update is rejected — no partial write of the non-connection fields.
    assert test_client.get(f"/api/data_sources/{w['ds_id']}", headers=hdr).json()["name"] == ds_before["name"]


@pytest.mark.e2e
def test_ds_manager_can_still_edit_agent_metadata(test_client, world):
    w = world
    hdr = _hdr(w["ds_manager"]["token"], w["org_id"])
    r = test_client.put(f"/api/data_sources/{w['ds_id']}",
                        json={"name": "renamed-agent", "description": "new description"}, headers=hdr)
    assert r.status_code == 200, r.text
    got = test_client.get(f"/api/data_sources/{w['ds_id']}", headers=hdr).json()
    assert got["name"] == "renamed-agent"
    assert got["description"] == "new description"


@pytest.mark.e2e
@pytest.mark.parametrize("which", ["sql_conn", "mcp_conn"])
def test_ds_manager_can_unlink_and_relink_connection(test_client, world, which):
    w = world
    hdr = _hdr(w["ds_manager"]["token"], w["org_id"])
    cid = w[which]["id"]
    base = f"/api/data_sources/{w['ds_id']}/connections/{cid}"

    with _skip_network():
        r = test_client.delete(base, headers=hdr)
        assert r.status_code == 200, r.text
        assert cid not in _ds_conn_ids(test_client, w)

        r = test_client.post(f"{base}?sync_tables=false", headers=hdr)
        assert r.status_code == 200, r.text
    assert cid in _ds_conn_ids(test_client, w)


@pytest.mark.e2e
@pytest.mark.parametrize("who", ["admin", "conn_admin"])
def test_connection_managers_can_update_connection_via_data_source(test_client, world, who):
    w = world
    hdr = _hdr(w[who]["token"], w["org_id"])
    with _skip_network():
        r = test_client.put(f"/api/data_sources/{w['ds_id']}",
                            json={"config": {"database": str(CHINOOK), "marker": who}}, headers=hdr)
    assert r.status_code == 200, r.text
    # The update is delegated to one of the agent's connections.
    markers = [(_conn_state(test_client, w, c["id"])[0] or {}).get("marker")
               for c in (w["sql_conn"], w["mcp_conn"])]
    assert who in markers
