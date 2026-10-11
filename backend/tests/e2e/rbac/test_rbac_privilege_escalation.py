"""
Privilege-escalation guard for RBAC mutation routes.

Invariant: a caller who is not a full admin can never grant (to anyone,
themselves included) a permission they do not already hold, and can never
change their own role assignments or group memberships. ``manage_members``
lets a delegate manage people; it must not be a path to ``full_admin_access``.

Covered grant paths:
  - POST /role-assignments           (system admin role, or any wider role)
  - POST /roles, PUT /roles/{id}     (custom role with permissions not held)
  - POST /groups/{id}/members        (joining a group that carries wider roles)

Full admins are unaffected, and a delegate can still grant within their own
permissions to other people.
"""
import uuid

import pytest


PEOPLE_MANAGER_PERMS = ["view_members", "manage_members"]


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def delegate_setup(
    enterprise_license, bootstrap_admin, invite_user_to_org, create_role, assign_role,
):
    """Org with an admin, a people-manager delegate, and a plain member."""
    admin = bootstrap_admin()
    org_id = admin["org_id"]

    mgr_role = create_role(
        name=f"people-mgr-{uuid.uuid4().hex[:6]}", permissions=PEOPLE_MANAGER_PERMS,
        user_token=admin["token"], org_id=org_id,
    )
    assert mgr_role.status_code == 200, mgr_role.json()

    delegate = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    resp = assign_role(
        role_id=mgr_role.json()["id"], principal_type="user",
        principal_id=delegate["user_id"], user_token=admin["token"], org_id=org_id,
    )
    assert resp.status_code == 200, resp.json()

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    return {"admin": admin, "org_id": org_id, "delegate": delegate, "member": member}


def _whoami_perms(whoami, token, org_id):
    org = next(o for o in whoami(token)["organizations"] if o["id"] == org_id)
    return set(org["permissions"])


@pytest.mark.e2e
@pytest.mark.parametrize("target", ["self", "other"])
def test_delegate_cannot_assign_admin_role(
    delegate_setup, get_system_role, assign_role, whoami, target,
):
    s = delegate_setup
    org_id, delegate = s["org_id"], s["delegate"]
    admin_role = get_system_role("admin", user_token=s["admin"]["token"], org_id=org_id)
    principal = delegate if target == "self" else s["member"]

    resp = assign_role(
        role_id=admin_role["id"], principal_type="user", principal_id=principal["user_id"],
        user_token=delegate["token"], org_id=org_id,
    )
    assert resp.status_code == 403, resp.json()
    assert "full_admin_access" not in _whoami_perms(whoami, principal["token"], org_id)


@pytest.mark.e2e
@pytest.mark.parametrize("perms", [
    ["full_admin_access"],
    ["view_members", "manage_connections"],
    ["manage_settings"],
])
def test_delegate_cannot_create_role_wider_than_self(delegate_setup, create_role, perms):
    s = delegate_setup
    resp = create_role(
        name=f"wide-{uuid.uuid4().hex[:6]}", permissions=perms,
        user_token=s["delegate"]["token"], org_id=s["org_id"],
    )
    assert resp.status_code == 403, resp.json()


@pytest.mark.e2e
def test_delegate_cannot_widen_existing_role(delegate_setup, create_role, update_role):
    s = delegate_setup
    role = create_role(
        name=f"narrow-{uuid.uuid4().hex[:6]}", permissions=["view_members"],
        user_token=s["admin"]["token"], org_id=s["org_id"],
    ).json()

    resp = update_role(
        role_id=role["id"], permissions=["view_members", "full_admin_access"],
        user_token=s["delegate"]["token"], org_id=s["org_id"],
    )
    assert resp.status_code == 403, resp.json()


@pytest.mark.e2e
@pytest.mark.parametrize("target", ["self", "other"])
def test_delegate_cannot_join_privileged_group(
    delegate_setup, create_group, get_system_role, assign_role, add_user_to_group, whoami, target,
):
    s = delegate_setup
    org_id, admin_token = s["org_id"], s["admin"]["token"]
    group = create_group(name=f"admins-{uuid.uuid4().hex[:6]}", user_token=admin_token, org_id=org_id)
    assert group.status_code == 200, group.json()
    group_id = group.json()["id"]
    admin_role = get_system_role("admin", user_token=admin_token, org_id=org_id)
    resp = assign_role(
        role_id=admin_role["id"], principal_type="group", principal_id=group_id,
        user_token=admin_token, org_id=org_id,
    )
    assert resp.status_code == 200, resp.json()
    principal = s["delegate"] if target == "self" else s["member"]

    resp = add_user_to_group(
        group_id=group_id, user_id=principal["user_id"],
        user_token=s["delegate"]["token"], org_id=org_id,
    )
    assert resp.status_code == 403, resp.json()
    assert "full_admin_access" not in _whoami_perms(whoami, principal["token"], org_id)


@pytest.mark.e2e
def test_delegate_cannot_change_own_assignments(delegate_setup, create_role, assign_role):
    """Even a role within the delegate's own permissions can't be self-assigned."""
    s = delegate_setup
    role = create_role(
        name=f"sub-{uuid.uuid4().hex[:6]}", permissions=["view_members"],
        user_token=s["admin"]["token"], org_id=s["org_id"],
    ).json()

    resp = assign_role(
        role_id=role["id"], principal_type="user", principal_id=s["delegate"]["user_id"],
        user_token=s["delegate"]["token"], org_id=s["org_id"],
    )
    assert resp.status_code == 403, resp.json()


@pytest.mark.e2e
def test_delegate_can_grant_within_own_permissions(delegate_setup, create_role, assign_role, whoami):
    s = delegate_setup
    org_id, delegate = s["org_id"], s["delegate"]

    role = create_role(
        name=f"sub-{uuid.uuid4().hex[:6]}", permissions=["view_members"],
        user_token=delegate["token"], org_id=org_id,
    )
    assert role.status_code == 200, role.json()

    resp = assign_role(
        role_id=role.json()["id"], principal_type="user", principal_id=s["member"]["user_id"],
        user_token=delegate["token"], org_id=org_id,
    )
    assert resp.status_code == 200, resp.json()
    assert "view_members" in _whoami_perms(whoami, s["member"]["token"], org_id)


@pytest.mark.e2e
def test_full_admin_can_still_grant_admin(delegate_setup, get_system_role, assign_role, whoami):
    s = delegate_setup
    org_id, admin_token = s["org_id"], s["admin"]["token"]
    admin_role = get_system_role("admin", user_token=admin_token, org_id=org_id)

    resp = assign_role(
        role_id=admin_role["id"], principal_type="user", principal_id=s["member"]["user_id"],
        user_token=admin_token, org_id=org_id,
    )
    assert resp.status_code == 200, resp.json()
    assert "full_admin_access" in _whoami_perms(whoami, s["member"]["token"], org_id)
