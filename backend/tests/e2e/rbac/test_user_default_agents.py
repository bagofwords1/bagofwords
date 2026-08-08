"""
E2E tests for the per-user default agent scope
(memberships.default_data_source_ids).

Behavior under test:
- an empty list is Auto, not "no agents": it is the default for a fresh
  membership and what the prompt box renders as Auto, so the backend must never
  substitute the whole roster for it;
- any member can pin/clear their own scope (`PUT /users/me/default_agents`) and
  read it back (`GET`), with order preserved and duplicates collapsed;
- the preference is per user and per organization — one user's scope never
  leaks into another's, and pinning in one org does not touch the other;
- setting is scoped to what the caller can see: ids for agents they can't see
  (or that don't exist) are dropped rather than 4xx, so a stale tab racing an
  agent deletion still saves the rest;
- resolution is lenient the same way the default-model preference is: an agent
  deleted after being pinned silently disappears from the stored scope, and a
  scope that loses every agent degrades to Auto instead of erroring.

Uses the RBAC cast fixtures.
"""
import uuid

import pytest


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _get_agents(test_client, token, org_id):
    resp = test_client.get("/api/users/me/default_agents", headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.text
    return resp.json()["data_source_ids"]


def _put_agents(test_client, token, org_id, ids):
    return test_client.put(
        "/api/users/me/default_agents",
        json={"data_source_ids": ids},
        headers=_headers(token, org_id),
    )


@pytest.fixture
def cast(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source):
    """Admin + member in one org, with a public agent both can see and a
    private one only the admin is a member of."""
    admin = bootstrap_admin("agentdef")
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    shared = sqlite_data_source(
        name=f"shared_{uuid.uuid4().hex[:6]}",
        user_token=admin["token"],
        org_id=admin["org_id"],
        is_public=True,
    )
    private = sqlite_data_source(
        name=f"private_{uuid.uuid4().hex[:6]}",
        user_token=admin["token"],
        org_id=admin["org_id"],
        is_public=False,
    )
    return {
        "admin": admin,
        "member": member,
        "org_id": admin["org_id"],
        "shared_id": shared["id"],
        "private_id": private["id"],
    }


# ── Auto is the default, and it is an empty list ─────────────────────────


@pytest.mark.e2e
def test_fresh_membership_defaults_to_auto(test_client, cast):
    """A member who never picked anything is on Auto — and Auto is empty, NOT
    every agent they can reach. Returning the roster here is what makes the
    prompt box open with everything checked."""
    assert _get_agents(test_client, cast["member"]["token"], cast["org_id"]) == []
    assert _get_agents(test_client, cast["admin"]["token"], cast["org_id"]) == []


@pytest.mark.e2e
def test_member_pins_and_clears_own_scope(test_client, cast):
    member_t, org = cast["member"]["token"], cast["org_id"]

    resp = _put_agents(test_client, member_t, org, [cast["shared_id"]])
    assert resp.status_code == 200, resp.text
    assert resp.json()["data_source_ids"] == [cast["shared_id"]]
    assert _get_agents(test_client, member_t, org) == [cast["shared_id"]]

    # Clearing is a first-class choice, and it lands back on Auto.
    resp = _put_agents(test_client, member_t, org, [])
    assert resp.status_code == 200, resp.text
    assert resp.json()["data_source_ids"] == []
    assert _get_agents(test_client, member_t, org) == []


@pytest.mark.e2e
def test_scope_keeps_order_and_collapses_duplicates(test_client, cast):
    admin_t, org = cast["admin"]["token"], cast["org_id"]
    ordered = [cast["private_id"], cast["shared_id"]]

    resp = _put_agents(test_client, admin_t, org, ordered + [cast["private_id"]])
    assert resp.status_code == 200, resp.text
    assert resp.json()["data_source_ids"] == ordered
    assert _get_agents(test_client, admin_t, org) == ordered


# ── per user, per org ────────────────────────────────────────────────────


@pytest.mark.e2e
def test_scope_is_per_user(test_client, cast):
    org = cast["org_id"]
    assert _put_agents(test_client, cast["admin"]["token"], org, [cast["shared_id"]]).status_code == 200

    # The other member is untouched by the admin's choice.
    assert _get_agents(test_client, cast["member"]["token"], org) == []


@pytest.mark.e2e
def test_scope_is_per_organization(test_client, bootstrap_admin, sqlite_data_source):
    """The scope lives on the membership, so each workspace carries its own —
    ids from one org would not even resolve in the other."""
    first = bootstrap_admin("agentdef_org1")
    second = bootstrap_admin("agentdef_org2")
    ds = sqlite_data_source(
        name=f"a_{uuid.uuid4().hex[:6]}",
        user_token=first["token"],
        org_id=first["org_id"],
        is_public=True,
    )

    assert _put_agents(test_client, first["token"], first["org_id"], [ds["id"]]).status_code == 200
    assert _get_agents(test_client, first["token"], first["org_id"]) == [ds["id"]]
    # A different org's membership starts on Auto and stays there.
    assert _get_agents(test_client, second["token"], second["org_id"]) == []


# ── access: writes are filtered, not rejected ────────────────────────────


@pytest.mark.e2e
def test_unknown_id_is_dropped_not_rejected(test_client, cast):
    """A stale tab racing an agent deletion should still save the rest of the
    scope rather than failing the whole write."""
    member_t, org = cast["member"]["token"], cast["org_id"]
    resp = _put_agents(test_client, member_t, org, [str(uuid.uuid4()), cast["shared_id"]])
    assert resp.status_code == 200, resp.text
    assert resp.json()["data_source_ids"] == [cast["shared_id"]]


@pytest.mark.e2e
def test_member_cannot_pin_an_agent_they_cannot_see(test_client, cast):
    """The private agent belongs to the admin only. A member naming it gets it
    dropped — the preference must not become a way to reference (or discover)
    an agent they have no access to."""
    member_t, org = cast["member"]["token"], cast["org_id"]
    resp = _put_agents(test_client, member_t, org, [cast["private_id"], cast["shared_id"]])
    assert resp.status_code == 200, resp.text
    assert resp.json()["data_source_ids"] == [cast["shared_id"]]

    # ...while the admin, who is a member of it, can.
    admin_resp = _put_agents(test_client, cast["admin"]["token"], org, [cast["private_id"]])
    assert admin_resp.status_code == 200, admin_resp.text
    assert admin_resp.json()["data_source_ids"] == [cast["private_id"]]


# ── stale scopes degrade to Auto rather than breaking ────────────────────


@pytest.mark.e2e
def test_deleted_agent_drops_out_and_empty_scope_reads_as_auto(test_client, cast):
    """Resolution is lenient: a pinned agent that goes away is pruned on read,
    and a scope that loses everything reads as Auto. A user preference must
    never leave the picker naming an agent that isn't there."""
    admin_t, org = cast["admin"]["token"], cast["org_id"]
    assert _put_agents(test_client, admin_t, org, [cast["private_id"], cast["shared_id"]]).status_code == 200

    del_resp = test_client.delete(
        f"/api/data_sources/{cast['private_id']}", headers=_headers(admin_t, org)
    )
    assert del_resp.status_code in (200, 204), del_resp.text
    assert _get_agents(test_client, admin_t, org) == [cast["shared_id"]]

    del_resp = test_client.delete(
        f"/api/data_sources/{cast['shared_id']}", headers=_headers(admin_t, org)
    )
    assert del_resp.status_code in (200, 204), del_resp.text
    assert _get_agents(test_client, admin_t, org) == []
