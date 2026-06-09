"""
E2E coverage for assigning roles and groups to *pending* memberships —
invites for users who haven't registered yet.

An org admin can pre-assign:
  - RBAC roles, via role-assignments with principal_type='membership'
  - groups, via group members with a membership_id

When the invitee registers, ``UserManager._attach_open_memberships`` →
``_materialize_pending_rbac`` rewrites those pending rows onto the new user,
so the permission resolver (which only knows 'user'/'group' principals) sees
the intended access on the very first request.
"""
import uuid

import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _whoami_org(whoami, token, org_id):
    info = whoami(token)
    return next(o for o in info["organizations"] if o["id"] == org_id)


def _invite_pending(test_client, admin, email=None):
    """Invite an email without registering it — returns the pending membership."""
    if email is None:
        email = f"pending_{uuid.uuid4().hex[:10]}@test.com"
    resp = test_client.post(
        f"/api/organizations/{admin['org_id']}/members",
        json={"organization_id": admin["org_id"], "email": email, "role": "member"},
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["user_id"] is None
    return {"email": email, "membership_id": body["id"]}


# ────────────────────────────────────────────────────────────────────
# Roles on pending memberships
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_assign_role_to_pending_membership_surfaces_in_members(
    test_client, bootstrap_admin, get_system_role, whoami
):
    """A role assigned to a pending invite shows up in GET /members."""
    admin = bootstrap_admin()
    pending = _invite_pending(test_client, admin)

    admin_role = get_system_role("admin", user_token=admin["token"], org_id=admin["org_id"])
    assign = test_client.post(
        f"/api/organizations/{admin['org_id']}/role-assignments",
        json={"role_id": admin_role["id"], "principal_type": "membership", "principal_id": pending["membership_id"]},
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert assign.status_code == 200, assign.text

    members = test_client.get(
        f"/api/organizations/{admin['org_id']}/members",
        headers=_hdr(admin["token"], admin["org_id"]),
    ).json()
    pending_member = next(m for m in members if m["id"] == pending["membership_id"])
    role_names = {r["name"] for r in pending_member["roles"]}
    assert "admin" in role_names
    assert all(r["source"] == "direct" for r in pending_member["roles"])


@pytest.mark.e2e
def test_pending_role_materializes_on_registration(
    test_client, bootstrap_admin, get_system_role, create_user, login_user, whoami
):
    """A role pre-assigned to a pending invite takes effect once the user registers."""
    admin = bootstrap_admin()
    pending = _invite_pending(test_client, admin)

    admin_role = get_system_role("admin", user_token=admin["token"], org_id=admin["org_id"])
    assign = test_client.post(
        f"/api/organizations/{admin['org_id']}/role-assignments",
        json={"role_id": admin_role["id"], "principal_type": "membership", "principal_id": pending["membership_id"]},
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert assign.status_code == 200, assign.text

    # Now the invitee registers and logs in.
    create_user(email=pending["email"], password="Test1234!")
    token = login_user(pending["email"], "Test1234!")

    org = _whoami_org(whoami, token, admin["org_id"])
    assert "full_admin_access" in set(org["permissions"])
    assert "admin" in set(org["roles"])

    # The pending 'membership' role assignment has been rewritten to 'user'.
    user_id = whoami(token)["id"]
    membership_assignments = test_client.get(
        f"/api/organizations/{admin['org_id']}/role-assignments?principal_type=membership&principal_id={pending['membership_id']}",
        headers=_hdr(admin["token"], admin["org_id"]),
    ).json()
    assert membership_assignments == []
    user_assignments = test_client.get(
        f"/api/organizations/{admin['org_id']}/role-assignments?principal_type=user&principal_id={user_id}",
        headers=_hdr(admin["token"], admin["org_id"]),
    ).json()
    assert any(a["role_id"] == admin_role["id"] for a in user_assignments)


@pytest.mark.e2e
def test_membership_principal_rejected_for_registered_member(
    test_client, bootstrap_admin, invite_user_to_org, get_system_role
):
    """Once a member is registered they must be addressed as a 'user' principal."""
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])

    admin_role = get_system_role("admin", user_token=admin["token"], org_id=admin["org_id"])
    resp = test_client.post(
        f"/api/organizations/{admin['org_id']}/role-assignments",
        json={"role_id": admin_role["id"], "principal_type": "membership", "principal_id": member["membership_id"]},
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.e2e
def test_membership_principal_unknown_membership_404(
    test_client, bootstrap_admin, get_system_role
):
    admin = bootstrap_admin()
    admin_role = get_system_role("admin", user_token=admin["token"], org_id=admin["org_id"])
    resp = test_client.post(
        f"/api/organizations/{admin['org_id']}/role-assignments",
        json={"role_id": admin_role["id"], "principal_type": "membership", "principal_id": str(uuid.uuid4())},
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert resp.status_code == 404, resp.text


# ────────────────────────────────────────────────────────────────────
# Groups on pending memberships (enterprise)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_add_pending_membership_to_group_and_list(
    test_client, bootstrap_admin, enterprise_license, create_group
):
    """A pending invite can be added to a group and is reflected in listings."""
    admin = bootstrap_admin()
    pending = _invite_pending(test_client, admin)

    group = create_group(name="early-birds", user_token=admin["token"], org_id=admin["org_id"]).json()

    add = test_client.post(
        f"/api/organizations/{admin['org_id']}/groups/{group['id']}/members",
        json={"membership_id": pending["membership_id"]},
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert add.status_code in (200, 201), add.text

    # list_groups exposes pending membership ids and counts them.
    groups = test_client.get(
        f"/api/organizations/{admin['org_id']}/groups",
        headers=_hdr(admin["token"], admin["org_id"]),
    ).json()
    g = next(x for x in groups if x["id"] == group["id"])
    assert pending["membership_id"] in g["member_membership_ids"]
    assert g["member_count"] == 1

    # group member list shows the pending entry.
    members = test_client.get(
        f"/api/organizations/{admin['org_id']}/groups/{group['id']}/members",
        headers=_hdr(admin["token"], admin["org_id"]),
    ).json()
    pending_entries = [m for m in members if m["pending"]]
    assert len(pending_entries) == 1
    assert pending_entries[0]["membership_id"] == pending["membership_id"]
    assert pending_entries[0]["user_email"] == pending["email"]

    # Duplicate add is rejected.
    dup = test_client.post(
        f"/api/organizations/{admin['org_id']}/groups/{group['id']}/members",
        json={"membership_id": pending["membership_id"]},
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert dup.status_code == 409, dup.text


@pytest.mark.e2e
def test_remove_pending_membership_from_group(
    test_client, bootstrap_admin, enterprise_license, create_group
):
    admin = bootstrap_admin()
    pending = _invite_pending(test_client, admin)
    group = create_group(name="rm-grp", user_token=admin["token"], org_id=admin["org_id"]).json()

    test_client.post(
        f"/api/organizations/{admin['org_id']}/groups/{group['id']}/members",
        json={"membership_id": pending["membership_id"]},
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    rm = test_client.delete(
        f"/api/organizations/{admin['org_id']}/groups/{group['id']}/members/{pending['membership_id']}",
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert rm.status_code == 204, rm.text

    groups = test_client.get(
        f"/api/organizations/{admin['org_id']}/groups",
        headers=_hdr(admin["token"], admin["org_id"]),
    ).json()
    g = next(x for x in groups if x["id"] == group["id"])
    assert pending["membership_id"] not in g["member_membership_ids"]
    assert g["member_count"] == 0


@pytest.mark.e2e
def test_group_add_requires_exactly_one_principal(
    test_client, bootstrap_admin, enterprise_license, create_group
):
    admin = bootstrap_admin()
    group = create_group(name="oneof-grp", user_token=admin["token"], org_id=admin["org_id"]).json()

    # Neither provided → 422 (schema validation).
    neither = test_client.post(
        f"/api/organizations/{admin['org_id']}/groups/{group['id']}/members",
        json={},
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert neither.status_code == 422, neither.text

    # Both provided → 422 (schema validation).
    both = test_client.post(
        f"/api/organizations/{admin['org_id']}/groups/{group['id']}/members",
        json={"user_id": admin["user_id"], "membership_id": str(uuid.uuid4())},
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert both.status_code == 422, both.text


@pytest.mark.e2e
def test_removing_pending_member_cleans_up_rbac(
    test_client, bootstrap_admin, enterprise_license, get_system_role, create_group
):
    """Deleting a pending invite drops its role assignments and group memberships."""
    admin = bootstrap_admin()
    pending = _invite_pending(test_client, admin)
    mid = pending["membership_id"]

    admin_role = get_system_role("admin", user_token=admin["token"], org_id=admin["org_id"])
    test_client.post(
        f"/api/organizations/{admin['org_id']}/role-assignments",
        json={"role_id": admin_role["id"], "principal_type": "membership", "principal_id": mid},
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    group = create_group(name="cleanup-grp", user_token=admin["token"], org_id=admin["org_id"]).json()
    test_client.post(
        f"/api/organizations/{admin['org_id']}/groups/{group['id']}/members",
        json={"membership_id": mid},
        headers=_hdr(admin["token"], admin["org_id"]),
    )

    # Remove the pending member.
    rm = test_client.delete(
        f"/api/organizations/{admin['org_id']}/members/{mid}",
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert rm.status_code == 204, rm.text

    # No dangling membership-principal role assignments.
    ras = test_client.get(
        f"/api/organizations/{admin['org_id']}/role-assignments?principal_type=membership&principal_id={mid}",
        headers=_hdr(admin["token"], admin["org_id"]),
    ).json()
    assert ras == []

    # No dangling pending group membership.
    groups = test_client.get(
        f"/api/organizations/{admin['org_id']}/groups",
        headers=_hdr(admin["token"], admin["org_id"]),
    ).json()
    g = next(x for x in groups if x["id"] == group["id"])
    assert mid not in g["member_membership_ids"]
    assert g["member_count"] == 0


@pytest.mark.e2e
def test_pending_group_membership_materializes_on_registration(
    test_client, bootstrap_admin, enterprise_license, create_group, create_role,
    create_user, login_user, whoami,
):
    """A pending invite added to a group inherits the group's role after registration."""
    admin = bootstrap_admin()
    pending = _invite_pending(test_client, admin)

    group = create_group(name="grant-team", user_token=admin["token"], org_id=admin["org_id"]).json()
    role = create_role(
        name="conn-mgr", permissions=["manage_connections", "view_members"],
        user_token=admin["token"], org_id=admin["org_id"],
    ).json()
    assign = test_client.post(
        f"/api/organizations/{admin['org_id']}/role-assignments",
        json={"role_id": role["id"], "principal_type": "group", "principal_id": group["id"]},
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert assign.status_code == 200, assign.text

    add = test_client.post(
        f"/api/organizations/{admin['org_id']}/groups/{group['id']}/members",
        json={"membership_id": pending["membership_id"]},
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert add.status_code in (200, 201), add.text

    # The pending member already shows the group-inherited role in /members.
    members = test_client.get(
        f"/api/organizations/{admin['org_id']}/members",
        headers=_hdr(admin["token"], admin["org_id"]),
    ).json()
    pending_member = next(m for m in members if m["id"] == pending["membership_id"])
    assert any(r["name"] == "conn-mgr" and r["source"].startswith("group:") for r in pending_member["roles"])

    # Register → the group membership is rewritten to the user, who inherits the role.
    create_user(email=pending["email"], password="Test1234!")
    token = login_user(pending["email"], "Test1234!")
    user_id = whoami(token)["id"]

    org = _whoami_org(whoami, token, admin["org_id"])
    assert "manage_connections" in set(org["permissions"])

    groups = test_client.get(
        f"/api/organizations/{admin['org_id']}/groups",
        headers=_hdr(admin["token"], admin["org_id"]),
    ).json()
    g = next(x for x in groups if x["id"] == group["id"])
    assert user_id in g["member_user_ids"]
    assert pending["membership_id"] not in g["member_membership_ids"]
