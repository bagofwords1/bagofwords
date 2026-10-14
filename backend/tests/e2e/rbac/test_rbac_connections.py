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
import asyncio
import uuid

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


@pytest.mark.e2e
def test_connection_grant_alone_can_create_agents(
    test_client, conn_world, invite_user_to_org, create_role, assign_role, grant_resource
):
    """A role whose ONLY authority is per-connection `create_data_sources`.

    Every persona in ``conn_world`` also holds org-level `create_data_source`,
    so the pure per-connection tier was never exercised: the route named only
    the org permission, and its per-connection check was unreachable. The role
    editor offers a "Create agents" checkbox scoped to one connection — that
    grant must actually build agents.

    Note the two distinct strings: org `create_data_source` (singular) vs
    per-connection `create_data_sources` (plural).
    """
    world = conn_world
    org_id, admin = world["org_id"], world["admin"]

    # No org-level permissions whatsoever.
    role = create_role(name="conn-only-builder", permissions=[],
                       user_token=admin["token"], org_id=org_id)
    assert role.status_code == 200, role.json()
    builder = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    asg = assign_role(role_id=role.json()["id"], principal_type="user",
                      principal_id=builder["user_id"], user_token=admin["token"], org_id=org_id)
    assert asg.status_code in (200, 201), asg.text
    g = grant_resource(resource_type="connection", resource_id=world["c1"],
                       principal_type="user", principal_id=builder["user_id"],
                       permissions=["create_data_sources"], user_token=admin["token"], org_id=org_id)
    assert g.status_code == 200, g.json()

    hdr = _hdr(builder["token"], org_id)

    ok = test_client.post("/api/data_sources", json=_agent_on([world["c1"]], "built-on-granted"),
                          headers=hdr)
    assert ok.status_code == 200, (
        "a per-connection create_data_sources grant must be able to build an agent "
        "on that connection: " + ok.text
    )

    # ...but only on the connection they were granted.
    denied = test_client.post("/api/data_sources", json=_agent_on([world["c2"]], "built-on-other"),
                              headers=hdr)
    assert denied.status_code == 403, denied.text

    # An agent with NO connection belongs to no connection, so the grant scopes
    # to nothing — that stays an org-level capability.
    no_conn = test_client.post(
        "/api/data_sources",
        json={"name": "built-without-connection", "type": "sqlite",
              "config": {}, "credentials": {}, "auth_policy": "system_only"},
        headers=hdr,
    )
    assert no_conn.status_code == 403, no_conn.text


@pytest.mark.e2e
def test_credential_metadata_is_admin_only(test_client, conn_world, grant_resource):
    """Visible connection access must not expose saved credential identifiers."""
    w = conn_world
    admin_headers = _hdr(w['admin']['token'], w['org_id'])
    response = test_client.put(f"/api/connections/{w['c1']}", headers=admin_headers,
                              json={'credentials': {'tenant_id': 'tenant-demo', 'user': 'reader', 'password': 'secret-demo'}})
    assert response.status_code == 200
    admin = test_client.get(f"/api/connections/{w['c1']}", headers=admin_headers)
    assert admin.status_code == 200
    assert admin.json()['credentials_meta'] == {'tenant_id': 'tenant-demo', 'user': 'reader'}
    grant = grant_resource(resource_type='data_source', resource_id=w['agent_on_c1'], principal_type='user', principal_id=w['plain']['user_id'], permissions=['manage'], user_token=w['admin']['token'], org_id=w['org_id'])
    assert grant.status_code == 200
    connection_grant = grant_resource(resource_type='connection', resource_id=w['c1'], principal_type='user', principal_id=w['plain']['user_id'], permissions=['manage_connection'], user_token=w['admin']['token'], org_id=w['org_id'])
    assert connection_grant.status_code == 200
    member = test_client.get(f"/api/connections/{w['c1']}", headers=_hdr(w['plain']['token'], w['org_id']))
    assert member.status_code == 200
    assert member.json()['credentials_meta'] is None
    assert member.json()['has_credentials'] is False

@pytest.mark.e2e
def test_connection_used_by_only_returns_accessible_agents(test_client, conn_world, grant_resource):
    world = conn_world
    admin, viewer = world['admin'], world['plain']
    org, connection = world['org_id'], world['c1']
    hidden = test_client.post('/api/data_sources', json=_agent_on([connection], name='Restricted finance'), headers=_hdr(admin['token'], org))
    assert hidden.status_code == 200, hidden.text
    granted = grant_resource(resource_type='data_source', resource_id=world['agent_on_c1'], principal_type='user', principal_id=viewer['user_id'], permissions=['query'], user_token=admin['token'], org_id=org)
    assert granted.status_code == 200, granted.text
    url = f'/api/connections/{connection}/accessible-agents'
    visible = test_client.get(url, headers=_hdr(viewer['token'], org))
    assert visible.status_code == 200, visible.text
    assert {item['id'] for item in visible.json()} == {world['agent_on_c1']}
    assert hidden.json()['name'] not in visible.text
    all_agents = test_client.get(url, headers=_hdr(admin['token'], org))
    assert all_agents.status_code == 200, all_agents.text
    assert {item['id'] for item in all_agents.json()} == {world['agent_on_c1'], hidden.json()['id']}
    inherited = test_client.get(url, headers=_hdr(world['manager']['token'], org))
    assert inherited.status_code == 200, inherited.text
    assert {item['id'] for item in inherited.json()} == {world['agent_on_c1'], hidden.json()['id']}
    denied = test_client.get(url, headers=_hdr(world['creator']['token'], org))
    assert denied.status_code in (200, 403), denied.text
    if denied.status_code == 200:
        assert denied.json() == []


# ── Connection read surfaces only reveal what the caller can see ─────────
#
# A viewer reaches /tables, /indexing, /accessible-agents and /files through
# ONE agent they can use. Those surfaces must not reveal the rest of the
# connection: tables no visible agent selected, agents they cannot see
# (private, or draft), or the raw indexing log. Connection managers (admins,
# connection-level manage grants) keep the full view.


def _viewer_world(test_client, w, grant_resource):
    admin, viewer, org = w['admin'], w['plain'], w['org_id']
    ah = _hdr(admin['token'], org)

    def _new_agent(name):
        r = test_client.post('/api/data_sources', json=_agent_on([w['c1']], name=name), headers=ah)
        assert r.status_code == 200, r.text
        return r.json()['id']

    def _grant(ds_id):
        g = grant_resource(resource_type='data_source', resource_id=ds_id, principal_type='user',
                           principal_id=viewer['user_id'], permissions=['query'],
                           user_token=admin['token'], org_id=org)
        assert g.status_code == 200, g.text

    private_agent = _new_agent(f'Private {uuid.uuid4().hex[:6]}')
    draft_agent = _new_agent(f'Draft {uuid.uuid4().hex[:6]}')
    _grant(w['agent_on_c1'])
    _grant(draft_agent)  # granted, but its lifecycle hides it from non-managers
    d = test_client.put(f'/api/data_sources/{draft_agent}', json={'publish_status': 'draft'}, headers=ah)
    assert d.status_code == 200, d.text
    return {'viewer_h': _hdr(viewer['token'], org), 'admin_h': ah,
            'private': private_agent, 'draft': draft_agent}


def _agent_tables(test_client, ds_id, headers):
    r = test_client.get(f'/api/data_sources/{ds_id}/full_schema', params={'page': 1, 'page_size': 500},
                        headers=headers)
    assert r.status_code == 200, r.text
    return r.json()['tables']


@pytest.mark.e2e
def test_accessible_agents_hides_agents_the_caller_cannot_see(test_client, conn_world, grant_resource):
    w = conn_world
    v = _viewer_world(test_client, w, grant_resource)
    url = f"/api/connections/{w['c1']}/accessible-agents"

    seen = test_client.get(url, headers=v['viewer_h'])
    assert seen.status_code == 200, seen.text
    assert {a['id'] for a in seen.json()} == {w['agent_on_c1']}
    assert set(seen.json()[0]) == {'id', 'name', 'icon'}

    # Admins and connection managers still get every linked agent.
    everyone = {w['agent_on_c1'], v['private'], v['draft']}
    for token in (w['admin']['token'], w['manager']['token']):
        r = test_client.get(url, headers=_hdr(token, w['org_id']))
        assert r.status_code == 200, r.text
        assert {a['id'] for a in r.json()} == everyone


@pytest.mark.e2e
def test_connection_tables_are_limited_to_the_callers_selected_tables(test_client, conn_world, grant_resource):
    w = conn_world
    v = _viewer_world(test_client, w, grant_resource)
    url = f"/api/connections/{w['c1']}/tables"

    full = test_client.get(url, headers=v['admin_h'])
    assert full.status_code == 200, full.text
    all_names = {t['name'] for t in full.json()}
    assert len(all_names) >= 3

    # The viewer's agent selects exactly two tables; the hidden agents select
    # a different one, which must not leak through them either.
    visible = sorted(all_names)[:2]
    other = sorted(all_names)[-1]
    for ds_id, activate in ((w['agent_on_c1'], visible), (v['private'], [other]), (v['draft'], [other])):
        current = [t['name'] for t in _agent_tables(test_client, ds_id, v['admin_h']) if t['is_active']]
        r = test_client.put(f'/api/data_sources/{ds_id}/update_tables_status',
                            json={'activate': activate, 'deactivate': [n for n in current if n not in activate]},
                            headers=v['admin_h'])
        assert r.status_code == 200, r.text

    seen = test_client.get(url, headers=v['viewer_h'])
    assert seen.status_code == 200, seen.text
    assert {t['name'] for t in seen.json()} == set(visible)
    assert set(seen.json()[0]) == {'id', 'name', 'column_count'}

    # Managers keep the whole catalog (they pick tables from it).
    for token in (w['admin']['token'], w['manager']['token']):
        r = test_client.get(url, headers=_hdr(token, w['org_id']))
        assert r.status_code == 200, r.text
        assert {t['name'] for t in r.json()} == all_names


@pytest.mark.e2e
def test_connection_indexing_redacts_hidden_agent_metadata_for_viewers(test_client, conn_world, grant_resource):
    w = conn_world
    v = _viewer_world(test_client, w, grant_resource)
    url = f"/api/connections/{w['c1']}/indexing?scope=org"

    # The finished run's stats count every linked agent it synced. This
    # fixture's run finished before the agents were linked, and no API reruns
    # it synchronously, so stamp what a post-link run records.
    async def _stamp():
        from sqlalchemy import select
        from app.dependencies import async_session_maker
        from app.models.connection_indexing import ConnectionIndexing
        async with async_session_maker() as db:
            row = (await db.execute(select(ConnectionIndexing).where(
                ConnectionIndexing.connection_id == w['c1']))).scalars().first()
            row.stats_json = {**(row.stats_json or {}), 'synced_domains': 3,
                              'unreadable_datasets': [{'name': 'secret_model'}],
                              'unreadable_dataset_count': 1}
            await db.commit()
    asyncio.run(_stamp())

    admin = test_client.get(url, headers=v['admin_h'])
    assert admin.status_code == 200, admin.text
    assert admin.json()['stats']['synced_domains'] == 3
    assert admin.json()['stats']['unreadable_dataset_count'] == 1
    assert admin.json()['events']

    seen = test_client.get(url, headers=v['viewer_h'])
    assert seen.status_code == 200, seen.text
    body = seen.json()
    assert body['status'] == admin.json()['status']
    assert body['stats']['synced_domains'] == 1  # only the agent they can see
    assert 'secret_model' not in seen.text
    assert body['events'] == []


@pytest.mark.e2e
def test_connection_files_requires_access_to_the_connection(test_client, conn_world):
    w = conn_world
    # c2 backs no agent and grants the plain member nothing.
    url = f"/api/connections/{w['c2']}/files"
    denied = test_client.get(url, headers=_hdr(w['plain']['token'], w['org_id']))
    assert denied.status_code == 403, denied.text
    # Admins pass the access check (sqlite has no file listing: 400, not 403).
    admin = test_client.get(url, headers=_hdr(w['admin']['token'], w['org_id']))
    assert admin.status_code == 400, admin.text
