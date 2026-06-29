"""
RBAC end-to-end coverage for the per-connection permission model.

Connection-scoped grants:
    manage_connection    — edit config / test / reindex / delete the connection
    create_data_sources  — create agents on this connection
    manage_data_sources  — manage ALL agents on this connection (implies create;
                           cascades to per-agent `manage`, ALL-connections)

World:
    admin            — full_admin (creates connections C1, C2 + agent A on C1)
    creator          — org create_data_source + connection `create_data_sources` on C1
    manager          — org create_data_source + connection `manage_data_sources` on C1
    plain            — org create_data_source only (no connection grant)
"""
import pytest
from pathlib import Path

CHINOOK = (Path(__file__).resolve().parents[2] / "config" / "chinook.sqlite").resolve()


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _agent_on(conn_ids, name=None):
    return {
        "name": name or f"agent-{'-'.join(c[:4] for c in conn_ids)}",
        "connection_ids": conn_ids,
        "generate_summary": False,
        "generate_conversation_starters": False,
        "generate_ai_rules": False,
    }


@pytest.fixture
def conn_world(
    test_client, bootstrap_admin, invite_user_to_org,
    create_role, assign_role, create_connection, grant_resource,
):
    if not CHINOOK.exists():
        pytest.skip(f"Missing SQLite fixture at {CHINOOK}")

    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]

    # Role granting org-level create_data_source (the "can create agents" switch).
    role = create_role(name="agent-creators", permissions=["create_data_source"],
                       user_token=admin["token"], org_id=org_id)
    assert role.status_code == 200, role.json()
    role_id = role.json()["id"]

    def _member_with_create():
        m = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
        r = assign_role(role_id=role_id, principal_type="user", principal_id=m["user_id"],
                        user_token=admin["token"], org_id=org_id)
        assert r.status_code in (200, 201), r.json()
        return m

    creator, manager, plain = _member_with_create(), _member_with_create(), _member_with_create()

    # Admin creates two connections.
    c1 = create_connection(name="conn-one", type="sqlite", config={"database": str(CHINOOK)},
                           credentials={}, user_token=admin["token"], org_id=org_id)
    c2 = create_connection(name="conn-two", type="sqlite", config={"database": str(CHINOOK)},
                           credentials={}, user_token=admin["token"], org_id=org_id)

    # Per-connection grants on C1.
    g1 = grant_resource(resource_type="connection", resource_id=c1["id"], principal_type="user",
                        principal_id=creator["user_id"], permissions=["create_data_sources"],
                        user_token=admin["token"], org_id=org_id)
    assert g1.status_code == 200, g1.json()
    g2 = grant_resource(resource_type="connection", resource_id=c1["id"], principal_type="user",
                        principal_id=manager["user_id"], permissions=["manage_data_sources"],
                        user_token=admin["token"], org_id=org_id)
    assert g2.status_code == 200, g2.json()

    # Admin's agent on C1 (admin owns it; manager will manage it via the cascade).
    a = test_client.post("/api/data_sources", json=_agent_on([c1["id"]]), headers=_hdr(admin["token"], org_id))
    assert a.status_code == 200, a.text

    return {
        "org_id": org_id, "admin": admin, "creator": creator, "manager": manager, "plain": plain,
        "c1": c1["id"], "c2": c2["id"], "agent_on_c1": a.json()["id"],
    }


@pytest.mark.e2e
def test_create_agent_on_connection_requires_create_grant(test_client, conn_world):
    """Building an agent on an existing connection needs per-connection
    create_data_sources. All three members hold org create_data_source, so this
    isolates the connection gate."""
    org = conn_world["org_id"]; c1 = conn_world["c1"]

    # plain: org create_data_source but no connection grant → denied.
    r = test_client.post("/api/data_sources", json=_agent_on([c1], name="plain-agent"),
                         headers=_hdr(conn_world["plain"]["token"], org))
    assert r.status_code == 403, r.text

    # creator (create_data_sources) and manager (manage_data_sources ⇒ create) → allowed.
    for who in ("creator", "manager"):
        r = test_client.post("/api/data_sources", json=_agent_on([c1], name=f"{who}-agent"),
                             headers=_hdr(conn_world[who]["token"], org))
        assert r.status_code == 200, f"{who}: {r.text}"


@pytest.mark.e2e
def test_manage_connection_gates_config_ops(test_client, conn_world):
    """Connection config ops (view detail / edit) require manage_connection —
    which create/manage-agents grants do NOT confer."""
    org = conn_world["org_id"]; c1 = conn_world["c1"]

    # creator & manager lack manage_connection → cannot view/edit the connection.
    for who in ("creator", "manager", "plain"):
        tok = conn_world[who]["token"]
        assert test_client.get(f"/api/connections/{c1}", headers=_hdr(tok, org)).status_code == 403
        assert test_client.put(f"/api/connections/{c1}", json={"name": "hijack"},
                               headers=_hdr(tok, org)).status_code == 403

    # admin (full_admin) can.
    assert test_client.get(f"/api/connections/{c1}", headers=_hdr(conn_world["admin"]["token"], org)).status_code == 200


@pytest.mark.e2e
def test_manage_data_sources_cascades_to_agents(test_client, conn_world):
    """manage_data_sources on a connection lets you manage every agent on it —
    edit the agent and its instructions — even ones you didn't create. create-
    only and ungranted members cannot."""
    org = conn_world["org_id"]; agent = conn_world["agent_on_c1"]

    # manager: can edit the admin-owned agent (cascade → data_source manage).
    r = test_client.put(f"/api/data_sources/{agent}", json={"description": "managed via connection"},
                        headers=_hdr(conn_world["manager"]["token"], org))
    assert r.status_code == 200, r.text
    # …and its instructions (manage ⇒ manage_instructions).
    r = test_client.post("/api/instructions",
                         json={"text": "rule", "status": "published", "category": "general",
                               "data_source_ids": [agent]},
                         headers=_hdr(conn_world["manager"]["token"], org))
    assert r.status_code == 200, r.text

    # creator (create only) and plain cannot manage someone else's agent.
    for who in ("creator", "plain"):
        r = test_client.put(f"/api/data_sources/{agent}", json={"description": "nope"},
                            headers=_hdr(conn_world[who]["token"], org))
        assert r.status_code == 403, f"{who}: {r.text}"


@pytest.mark.e2e
def test_multi_connection_manage_is_all_or_nothing(test_client, conn_world):
    """A multi-connection agent is only managed via the cascade if you hold
    manage_data_sources on EVERY connection it uses (ALL-connections)."""
    org = conn_world["org_id"]; c1 = conn_world["c1"]; c2 = conn_world["c2"]; manager = conn_world["manager"]

    # Admin builds an agent spanning C1 + C2.
    a = test_client.post("/api/data_sources", json=_agent_on([c1, c2]), headers=_hdr(conn_world["admin"]["token"], org))
    assert a.status_code == 200, a.text
    agent = a.json()["id"]

    # manager has manage_data_sources on C1 only → cannot manage the C1+C2 agent.
    r = test_client.put(f"/api/data_sources/{agent}", json={"description": "partial"}, headers=_hdr(manager["token"], org))
    assert r.status_code == 403, r.text

    # Grant manage_data_sources on C2 too → now manager can manage it.
    g = test_client.post(
        f"/api/organizations/{org}/resource-grants",
        json={"resource_type": "connection", "resource_id": c2, "principal_type": "user",
              "principal_id": manager["user_id"], "permissions": ["manage_data_sources"]},
        headers=_hdr(conn_world["admin"]["token"], org),
    )
    assert g.status_code == 200, g.text
    r = test_client.put(f"/api/data_sources/{agent}", json={"description": "now managed"}, headers=_hdr(manager["token"], org))
    assert r.status_code == 200, r.text


@pytest.mark.e2e
def test_connection_creator_is_auto_granted(test_client, conn_world):
    """A member who creates an agent with a brand-new inline connection is
    auto-granted control of that connection — they can view/edit it."""
    org = conn_world["org_id"]; creator = conn_world["creator"]

    # creator builds an agent with an inline new connection (no connection_ids).
    a = test_client.post("/api/data_sources", json={
        "name": "creators-own", "type": "sqlite", "config": {"database": str(CHINOOK)},
        "credentials": {}, "auth_policy": "system_only",
        "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False,
    }, headers=_hdr(creator["token"], org))
    assert a.status_code == 200, a.text
    new_conn_id = a.json()["connections"][0]["id"]

    # As the connection's creator they hold manage_connection → can view its detail.
    assert test_client.get(f"/api/connections/{new_conn_id}", headers=_hdr(creator["token"], org)).status_code == 200
