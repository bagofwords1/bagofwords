"""
RBAC e2e coverage for the org-level ``create_connections`` permission
("connection creator").

Contract:
  - ``create_connections`` lets a member create connections. Whoever creates a
    connection owns it: creation writes a per-connection grant
    (``grant_connection_owner``) of ``manage_connection`` (edit / test /
    reindex / delete / tools) and ``manage_data_sources`` (manage every agent
    fully backed by it, including agents other people built; implies
    ``create_data_sources`` — build agents on it).
  - Ownership confers nothing on connections the user did not create — they do
    not see them in the list and get 403 on their detail/config routes.
  - Org connection admins (``manage_connections``) and full admins still see and
    manage every connection; ``manage_connections`` implies ``create_connections``.

World:
    admin    — full admin; owns connection ``admin_conn`` + a PRIVATE agent on it
    builder  — role with ONLY ``create_connections``
    builder2 — a second connection creator (peer isolation)
    conn_admin — role with ``manage_connections``
    plain    — member with no extra permissions
"""
from pathlib import Path

import pytest

from tests.fixtures.connection_tool import _skip_network

CHINOOK = (Path(__file__).resolve().parents[2] / "config" / "chinook.sqlite").resolve()


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _sqlite_conn(name):
    return {"name": name, "type": "sqlite", "config": {"database": str(CHINOOK)}, "credentials": {}}


def _mcp_conn(name):
    return {
        "name": name, "type": "mcp",
        "config": {"server_url": "http://mock-mcp:3000/mcp", "transport": "sse"},
        "credentials": {}, "auth_policy": "system_only",
    }


def _agent_on(conn_ids, name):
    return {
        "name": name, "connection_ids": conn_ids, "is_public": False,
        "generate_summary": False, "generate_conversation_starters": False,
        "generate_ai_rules": False,
    }


@pytest.fixture
def world(test_client, bootstrap_admin, invite_user_to_org, create_role, assign_role):
    if not CHINOOK.exists():
        pytest.skip(f"Missing SQLite fixture at {CHINOOK}")

    admin = bootstrap_admin("admin")
    org = admin["org_id"]

    def _member(perms):
        m = invite_user_to_org(org_id=org, admin_token=admin["token"])
        if perms:
            role = create_role(name=f"r-{m['user_id'][:8]}", permissions=perms,
                               user_token=admin["token"], org_id=org)
            assert role.status_code == 200, role.json()
            r = assign_role(role_id=role.json()["id"], principal_type="user",
                            principal_id=m["user_id"], user_token=admin["token"], org_id=org)
            assert r.status_code in (200, 201), r.json()
        return m

    builder = _member(["create_connections"])
    builder2 = _member(["create_connections"])
    conn_admin = _member(["manage_connections"])
    plain = _member([])

    r = test_client.post("/api/connections", json=_sqlite_conn("admin-conn"), headers=_hdr(admin["token"], org))
    assert r.status_code == 200, r.text
    admin_conn = r.json()["id"]
    a = test_client.post("/api/data_sources", json=_agent_on([admin_conn], "admin-private-agent"),
                         headers=_hdr(admin["token"], org))
    assert a.status_code == 200, a.text

    return {
        "org": org, "admin": admin, "builder": builder, "builder2": builder2,
        "conn_admin": conn_admin, "plain": plain, "admin_conn": admin_conn,
    }


def _create(test_client, world, who, payload):
    with _skip_network():
        return test_client.post("/api/connections", json=payload,
                                headers=_hdr(world[who]["token"], world["org"]))


def _list_ids(test_client, world, who):
    r = test_client.get("/api/connections", headers=_hdr(world[who]["token"], world["org"]))
    assert r.status_code == 200, r.text
    return {c["id"] for c in r.json()}


# ── Creating ─────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_create_connections_permission_gates_connection_creation(test_client, world):
    org = world["org"]
    # No permission → cannot create or pre-test a connection.
    assert _create(test_client, world, "plain", _sqlite_conn("nope")).status_code == 403
    r = test_client.post("/api/connections/test-params", json=_sqlite_conn("nope"),
                         headers=_hdr(world["plain"]["token"], org))
    assert r.status_code == 403

    r = test_client.post("/api/data_sources/test_connection", json=_sqlite_conn("nope"),
                         headers=_hdr(world["plain"]["token"], org))
    assert r.status_code == 403

    # create_connections and manage_connections (which implies it) → allowed.
    # Both pre-save test routes: the connection form uses /data_sources/test_connection.
    for who in ("builder", "conn_admin"):
        for route in ("/api/connections/test-params", "/api/data_sources/test_connection"):
            r = test_client.post(route, json=_sqlite_conn("t"), headers=_hdr(world[who]["token"], org))
            assert r.status_code == 200, f"{who} {route}: {r.text}"
            assert r.json().get("success") is True, f"{who} {route}: {r.text}"
        r = _create(test_client, world, who, _sqlite_conn(f"{who}-conn"))
        assert r.status_code == 200, f"{who}: {r.text}"


@pytest.mark.e2e
def test_creator_owns_connection_in_resolved_permissions(test_client, world, whoami):
    r = _create(test_client, world, "builder", _sqlite_conn("mine"))
    assert r.status_code == 200, r.text
    cid = r.json()["id"]

    me = whoami(world["builder"]["token"])
    org = next(o for o in me["organizations"] if str(o["id"]) == str(world["org"]))
    perms = set(org["resource_permissions"].get(f"connection:{cid}", []))
    assert {"manage_connection", "manage_data_sources"} <= perms
    # Nothing on a connection the builder did not create.
    assert f"connection:{world['admin_conn']}" not in org["resource_permissions"]


# ── Managing own vs others' connections ───────────────────────────────────

@pytest.mark.e2e
def test_owner_manages_own_connection_but_not_others(test_client, world):
    org = world["org"]
    tok = world["builder"]["token"]
    mine = _create(test_client, world, "builder", _sqlite_conn("mine")).json()["id"]
    peer = _create(test_client, world, "builder2", _sqlite_conn("peer")).json()["id"]

    detail = test_client.get(f"/api/connections/{mine}", headers=_hdr(tok, org))
    assert detail.status_code == 200, detail.text
    # The owner gets the edit view (config), not the redacted member view.
    assert detail.json()["config"].get("database")
    r = test_client.put(f"/api/connections/{mine}", json={"name": "mine-renamed"}, headers=_hdr(tok, org))
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "mine-renamed"
    assert test_client.post(f"/api/connections/{mine}/test", headers=_hdr(tok, org)).status_code == 200

    for other in (world["admin_conn"], peer):
        assert test_client.get(f"/api/connections/{other}", headers=_hdr(tok, org)).status_code == 403
        assert test_client.put(f"/api/connections/{other}", json={"name": "hijack"},
                               headers=_hdr(tok, org)).status_code == 403
        assert test_client.post(f"/api/connections/{other}/test", headers=_hdr(tok, org)).status_code == 403
        assert test_client.delete(f"/api/connections/{other}", headers=_hdr(tok, org)).status_code == 403

    assert test_client.delete(f"/api/connections/{mine}", headers=_hdr(tok, org)).status_code == 200


# ── Visibility ────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_connection_list_visibility_by_role(test_client, world):
    mine = _create(test_client, world, "builder", _sqlite_conn("mine")).json()["id"]
    peer = _create(test_client, world, "builder2", _sqlite_conn("peer")).json()["id"]
    every = {mine, peer, world["admin_conn"]}

    # Connection creators see only their own connections (admin's agent is private).
    assert _list_ids(test_client, world, "builder") & every == {mine}
    assert _list_ids(test_client, world, "builder2") & every == {peer}
    assert _list_ids(test_client, world, "plain") & every == set()
    # Full admins and org connection admins see every connection.
    assert every <= _list_ids(test_client, world, "admin")
    assert every <= _list_ids(test_client, world, "conn_admin")


@pytest.mark.e2e
def test_connection_admin_still_manages_every_connection(test_client, world):
    org = world["org"]
    peer = _create(test_client, world, "builder2", _sqlite_conn("peer")).json()["id"]
    for who in ("conn_admin", "admin"):
        tok = world[who]["token"]
        for cid in (peer, world["admin_conn"]):
            assert test_client.get(f"/api/connections/{cid}", headers=_hdr(tok, org)).status_code == 200, who
            r = test_client.put(f"/api/connections/{cid}", json={"name": f"renamed-by-{who}-{cid[:6]}"}, headers=_hdr(tok, org))
            assert r.status_code == 200, f"{who}: {r.text}"


# ── Agents inside owned connections ───────────────────────────────────────

@pytest.mark.e2e
def test_owner_builds_agents_only_on_own_connection(test_client, world):
    """The builder has no org `create_data_source`; owning the connection is
    what lets them build agents on it."""
    org = world["org"]; tok = world["builder"]["token"]
    mine = _create(test_client, world, "builder", _sqlite_conn("mine")).json()["id"]

    r = test_client.post("/api/data_sources", json=_agent_on([mine], "builder-agent"), headers=_hdr(tok, org))
    assert r.status_code == 200, r.text
    agent = r.json()["id"]
    r = test_client.put(f"/api/data_sources/{agent}", json={"description": "edited by owner"}, headers=_hdr(tok, org))
    assert r.status_code == 200, r.text

    r = test_client.post("/api/data_sources", json=_agent_on([world["admin_conn"]], "nope"), headers=_hdr(tok, org))
    assert r.status_code == 403, r.text


@pytest.mark.e2e
def test_owner_manages_agents_others_built_on_their_connection(test_client, world):
    org = world["org"]
    mine = _create(test_client, world, "builder", _sqlite_conn("mine")).json()["id"]
    # The admin builds a private agent on the builder's connection.
    r = test_client.post("/api/data_sources", json=_agent_on([mine], "admin-on-builder-conn"),
                         headers=_hdr(world["admin"]["token"], org))
    assert r.status_code == 200, r.text
    agent = r.json()["id"]

    r = test_client.put(f"/api/data_sources/{agent}", json={"description": "managed by connection owner"},
                        headers=_hdr(world["builder"]["token"], org))
    assert r.status_code == 200, r.text
    # A peer creator has no say over it.
    r = test_client.put(f"/api/data_sources/{agent}", json={"description": "peer"},
                        headers=_hdr(world["builder2"]["token"], org))
    assert r.status_code == 403, r.text


# ── Tools (MCP / custom API) ──────────────────────────────────────────────

def _tools(test_client, world, who, cid):
    with _skip_network():
        r = test_client.post(f"/api/connections/{cid}/refresh-tools", headers=_hdr(world[who]["token"], world["org"]))
    return r


@pytest.mark.e2e
def test_owner_manages_tools_on_own_connection_only(test_client, world):
    org = world["org"]; tok = world["builder"]["token"]
    mine = _create(test_client, world, "builder", _mcp_conn("my-mcp"))
    assert mine.status_code == 200, mine.text
    mine = mine.json()["id"]
    theirs = _create(test_client, world, "admin", _mcp_conn("admin-mcp")).json()["id"]

    r = _tools(test_client, world, "builder", mine)
    assert r.status_code == 200, r.text
    my_tools = [t["id"] for t in r.json()]
    assert my_tools
    r = test_client.put(f"/api/connections/{mine}/tools/batch", json={"tool_ids": my_tools, "is_enabled": False},
                        headers=_hdr(tok, org))
    assert r.status_code == 200, r.text
    r = test_client.put(f"/api/connections/{mine}/tools/{my_tools[0]}", json={"is_enabled": True},
                        headers=_hdr(tok, org))
    assert r.status_code == 200, r.text

    # Someone else's connection: no refresh, no tool edits.
    assert _tools(test_client, world, "builder", theirs).status_code == 403
    their_tools = [t["id"] for t in _tools(test_client, world, "admin", theirs).json()]
    assert their_tools
    assert test_client.put(f"/api/connections/{theirs}/tools/batch",
                           json={"tool_ids": their_tools, "is_enabled": False},
                           headers=_hdr(tok, org)).status_code == 403
    # Smuggling another connection's tool ids through an owned connection's URL.
    r = test_client.put(f"/api/connections/{mine}/tools/batch", json={"tool_ids": their_tools, "is_enabled": False},
                        headers=_hdr(tok, org))
    assert r.status_code in (400, 404), r.text
    r = test_client.put(f"/api/connections/{mine}/tools/{their_tools[0]}", json={"is_enabled": False},
                        headers=_hdr(tok, org))
    assert r.status_code in (400, 404), r.text
    # The admin's tools were not touched.
    listed = test_client.get(f"/api/connections/{theirs}/tools", headers=_hdr(world["admin"]["token"], org)).json()
    assert all(t["is_enabled"] for t in listed)


# ── Inline connection created through the new-agent form ─────────────────

@pytest.mark.e2e
def test_inline_connection_from_agent_form_is_owned_by_its_creator(
    test_client, world, create_role, assign_role, invite_user_to_org,
):
    org = world["org"]; admin = world["admin"]
    m = invite_user_to_org(org_id=org, admin_token=admin["token"])
    role = create_role(name="agent-only", permissions=["create_data_source"], user_token=admin["token"], org_id=org)
    assign_role(role_id=role.json()["id"], principal_type="user", principal_id=m["user_id"],
                user_token=admin["token"], org_id=org)

    payload = {
        "name": "inline-agent", "type": "sqlite", "config": {"database": str(CHINOOK)}, "credentials": {},
        "is_public": False, "generate_summary": False, "generate_conversation_starters": False,
        "generate_ai_rules": False,
    }
    r = test_client.post("/api/data_sources", json=payload, headers=_hdr(m["token"], org))
    assert r.status_code == 200, r.text
    ds = test_client.get(f"/api/data_sources/{r.json()['id']}", headers=_hdr(m["token"], org)).json()
    cid = ds["connections"][0]["id"]

    assert test_client.get(f"/api/connections/{cid}", headers=_hdr(m["token"], org)).status_code == 200
    assert test_client.get(f"/api/connections/{cid}", headers=_hdr(world["builder"]["token"], org)).status_code == 403


# ── Resolver parity ───────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_single_and_bulk_resolvers_agree_on_connection_ownership(test_client, world):
    """Route checks and whoami resolve through two code paths (per-org and
    multi-org). Both must give the creator the owner tier on their connection —
    and the `manage` cascade to agents built on it — and nothing on others."""
    from app.dependencies import async_session_maker
    from app.core.permission_resolver import resolve_permissions, resolve_permissions_bulk

    org = world["org"]
    mine = _create(test_client, world, "builder", _sqlite_conn("mine")).json()["id"]
    r = test_client.post("/api/data_sources", json=_agent_on([mine], "admin-on-mine"),
                         headers=_hdr(world["admin"]["token"], org))
    assert r.status_code == 200, r.text
    agent = r.json()["id"]

    uid = str(world["builder"]["user_id"])
    async with async_session_maker() as session:
        single = await resolve_permissions(session, uid, str(org))
    async with async_session_maker() as session:
        bulk = (await resolve_permissions_bulk(session, uid, [str(org)]))[str(org)]

    for resolved in (single, bulk):
        for perm in ("manage_connection", "create_data_sources", "manage_data_sources"):
            assert resolved.has_resource_permission("connection", mine, perm), perm
            assert not resolved.has_resource_permission("connection", world["admin_conn"], perm), perm
        assert resolved.has_resource_permission("data_source", agent, "manage")
    assert single.resource_permissions == bulk.resource_permissions
