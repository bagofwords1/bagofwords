"""Removing an agent member works for every kind of member the list shows.

Contract: anything ``GET /data_sources/{id}/members`` lists, a manager of the
agent can remove with ``DELETE /data_sources/{id}/members/{principal_id}``
(passing ``principal_type``), and after a fresh listing it is gone while every
other member is untouched. Members reach the list by several write paths —
the agent's own "Add member" (users and groups), the org resource-grant API
(no legacy membership row), and a role's resource grants — so each is covered.
"""
import pytest

from tests.e2e.rbac.test_rbac_agent_manager_stories import group_world, _hdr  # noqa: F401


def _members(test_client, agent_id, token, org_id):
    r = test_client.get(f"/api/data_sources/{agent_id}/members", headers=_hdr(token, org_id))
    assert r.status_code == 200, r.text
    return {(m["principal_type"], m["principal_id"]) for m in r.json()}


@pytest.fixture
def populated_agent(test_client, group_world, create_group, create_role):
    """Agent owned by m1 with one member of each kind, added via each write path."""
    org_id = group_world["org_id"]
    admin, m1 = group_world["admin"], group_world["m1"]
    agent_id = group_world["agent1"]["id"]

    groups = []
    for name in ("ops-team", "support-team"):
        g = create_group(name=name, user_token=admin["token"], org_id=org_id)
        assert g.status_code == 200, g.text
        groups.append(g.json()["id"])

    # Owner adds a user and two groups through the agent's own members endpoint.
    added = [("user", group_world["outsider"]["user_id"])] + [("group", gid) for gid in groups]
    for ptype, pid in added:
        r = test_client.post(
            f"/api/data_sources/{agent_id}/members",
            json={"principal_type": ptype, "principal_id": pid},
            headers=_hdr(m1["token"], org_id),
        )
        assert r.status_code in (200, 201), r.text

    # A user granted through the org resource-grant API: no legacy membership row.
    r = test_client.post(
        f"/api/organizations/{org_id}/resource-grants",
        json={"resource_type": "data_source", "resource_id": agent_id,
              "principal_type": "user", "principal_id": group_world["m2"]["user_id"],
              "permissions": []},
        headers=_hdr(admin["token"], org_id),
    )
    assert r.status_code == 200, r.text

    # A role whose resource grants include this agent.
    role = create_role(name="agent-readers", permissions=[], user_token=admin["token"], org_id=org_id)
    assert role.status_code == 200, role.text
    role_id = role.json()["id"]
    r = test_client.put(
        f"/api/organizations/{org_id}/roles/{role_id}",
        json={"resource_grants": [{"resource_type": "data_source", "resource_id": agent_id, "permissions": []}]},
        headers=_hdr(admin["token"], org_id),
    )
    assert r.status_code == 200, r.text

    removable = added + [("user", group_world["m2"]["user_id"]), ("role", role_id)]
    listed = _members(test_client, agent_id, m1["token"], org_id)
    assert set(removable) <= listed, listed
    return {"org_id": org_id, "owner": m1, "agent_id": agent_id, "removable": removable}


def test_manager_can_remove_every_kind_of_listed_member(test_client, populated_agent):
    org_id, agent_id = populated_agent["org_id"], populated_agent["agent_id"]
    token = populated_agent["owner"]["token"]
    remaining = _members(test_client, agent_id, token, org_id)

    for ptype, pid in populated_agent["removable"]:
        r = test_client.delete(
            f"/api/data_sources/{agent_id}/members/{pid}",
            params={"principal_type": ptype},
            headers=_hdr(token, org_id),
        )
        assert r.status_code == 204, (ptype, r.text)
        remaining.discard((ptype, pid))
        # Gone on a fresh read, and nobody else was removed with it.
        assert _members(test_client, agent_id, token, org_id) == remaining


def test_remove_matches_principal_type(test_client, populated_agent):
    """A delete for the wrong type is a 404 and leaves the member in place."""
    org_id, agent_id = populated_agent["org_id"], populated_agent["agent_id"]
    token = populated_agent["owner"]["token"]
    ptype, group_id = next(m for m in populated_agent["removable"] if m[0] == "group")

    r = test_client.delete(
        f"/api/data_sources/{agent_id}/members/{group_id}",
        params={"principal_type": "user"},
        headers=_hdr(token, org_id),
    )
    assert r.status_code == 404, r.text
    assert (ptype, group_id) in _members(test_client, agent_id, token, org_id)


def test_remove_without_principal_type_defaults_to_user(test_client, populated_agent):
    """Older clients that omit principal_type keep removing users."""
    org_id, agent_id = populated_agent["org_id"], populated_agent["agent_id"]
    token = populated_agent["owner"]["token"]
    user_id = next(pid for t, pid in populated_agent["removable"] if t == "user")

    r = test_client.delete(f"/api/data_sources/{agent_id}/members/{user_id}", headers=_hdr(token, org_id))
    assert r.status_code == 204, r.text
    assert ("user", user_id) not in _members(test_client, agent_id, token, org_id)
