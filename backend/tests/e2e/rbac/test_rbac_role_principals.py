"""
Role-as-principal and dual-access-path RBAC tests.

The RBAC architecture in this branch has two effective access paths:

  1. Role-based: org membership ``role`` -> ROLES_PERMISSIONS lookup -> permission set
  2. DataSourceMembership: per-DS grant on a private data source

This file verifies:

  - Role transitions take effect on the very next request (no stale cache).
  - Granting/revoking DSM takes effect immediately.
  - The two paths are independent: DSM grant alone does not unlock org-level
    admin permissions.
  - Cross-org isolation: a user with admin role in their own org cannot use
    that role to act on another org.
  - The decorator's ``not is a member of this organization`` rejection path
    fires when the X-Organization-Id header references an org the user is
    not a member of.
"""
import pytest


def _h(token, org_id):
    return {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }


@pytest.mark.e2e
def test_role_change_takes_effect_immediately(
    test_client, rbac_principals, set_member_role,
):
    """Promoting a member to admin grants admin perms on the next request;
    demoting back to member revokes them."""
    org_id = rbac_principals["org_id"]
    admin_token = rbac_principals["admin_token"]
    member = rbac_principals["principals"]["member"]

    # Member: cannot create a build (admin perm)
    pre = test_client.post(
        "/api/builds",
        json={"source": "manual"},
        headers=_h(member["token"], org_id),
    )
    assert pre.status_code == 403, pre.json()

    # Promote to admin
    set_member_role(
        admin_token=admin_token,
        org_id=org_id,
        membership_id=member["membership_id"],
        role="admin",
    )

    # Now succeeds
    promoted = test_client.post(
        "/api/builds",
        json={"source": "manual"},
        headers=_h(member["token"], org_id),
    )
    assert promoted.status_code == 200, promoted.json()

    # Demote back to member
    set_member_role(
        admin_token=admin_token,
        org_id=org_id,
        membership_id=member["membership_id"],
        role="member",
    )

    # Should be 403 again immediately
    demoted = test_client.post(
        "/api/builds",
        json={"source": "manual"},
        headers=_h(member["token"], org_id),
    )
    assert demoted.status_code == 403, (
        f"After demoting to member, expected 403, got {demoted.status_code}"
    )


@pytest.mark.e2e
def test_ds_membership_grant_does_not_imply_org_admin(
    test_client, rbac_principals,
):
    """Being granted membership on a private DS does not unlock org-level
    admin permissions like update_data_source — admin role is still required."""
    org_id = rbac_principals["org_id"]
    p = rbac_principals["principals"]["ds_a_member"]
    ds_id = rbac_principals["ds_a"]["id"]

    # Can GET the data source they have membership on
    get_ds = test_client.get(
        f"/api/data_sources/{ds_id}", headers=_h(p["token"], org_id)
    )
    assert get_ds.status_code == 200, get_ds.json()

    # Cannot PUT it (update_data_source is admin-only)
    upd = test_client.put(
        f"/api/data_sources/{ds_id}",
        json={"description": "ds member trying to update"},
        headers=_h(p["token"], org_id),
    )
    assert upd.status_code == 403, (
        f"DSM does not imply update_data_source: expected 403, got {upd.status_code}"
    )

    # Cannot manage the DS's memberships either
    upd_mem = test_client.post(
        f"/api/data_sources/{ds_id}/members",
        json={"principal_type": "user", "principal_id": p["user_id"]},
        headers=_h(p["token"], org_id),
    )
    assert upd_mem.status_code == 403, upd_mem.json()


@pytest.mark.e2e
def test_outsider_role_in_own_org_does_not_apply_to_other_org(
    test_client, rbac_principals,
):
    """An outsider is admin of their own org but is rejected from this org's
    routes via the membership-required check in @requires_permission."""
    org_id = rbac_principals["org_id"]
    outsider = rbac_principals["principals"]["outsider"]

    # Verify the outsider IS admin of their own org by hitting an endpoint
    # in their own org. Use the eval admin gate as the canary.
    own = test_client.get(
        "/api/tests/suites",
        headers=_h(outsider["token"], outsider["own_org_id"]),
    )
    assert own.status_code == 200, (
        f"outsider in own org should be admin: {own.status_code}"
    )

    # Now hit the same endpoint with the *primary* org id — should be 403.
    cross = test_client.get(
        "/api/tests/suites", headers=_h(outsider["token"], org_id)
    )
    assert cross.status_code in (403, 404), (
        f"outsider should be rejected from primary org's tests endpoint: "
        f"got {cross.status_code}"
    )


@pytest.mark.e2e
def test_x_organization_id_header_must_match_membership(
    test_client, rbac_principals,
):
    """A valid token plus an X-Organization-Id pointing at an org the user
    is not a member of must be rejected."""
    outsider = rbac_principals["principals"]["outsider"]
    primary_org_id = rbac_principals["org_id"]

    resp = test_client.get(
        "/api/data_sources",
        headers={
            "Authorization": f"Bearer {outsider['token']}",
            "X-Organization-Id": str(primary_org_id),
        },
    )
    assert resp.status_code in (403, 404), (
        f"Bogus X-Organization-Id should be rejected: got {resp.status_code}"
    )


@pytest.mark.e2e
def test_revoking_org_membership_revokes_all_access(
    test_client, rbac_principals, invite_user_to_org,
):
    """When a user's org membership is removed, every subsequent call with
    their token returns 403 'not a member of this organization'."""
    org_id = rbac_principals["org_id"]
    admin_token = rbac_principals["admin_token"]

    # Bring in a fresh victim so we don't break the rbac_principals cast.
    victim = invite_user_to_org(admin_token=admin_token, org_id=org_id)

    # Sanity: while a member, they can list data sources
    pre = test_client.get(
        "/api/data_sources", headers=_h(victim["token"], org_id)
    )
    assert pre.status_code == 200, pre.json()

    # Admin removes the membership
    remove = test_client.delete(
        f"/api/organizations/{org_id}/members/{victim['membership_id']}",
        headers=_h(admin_token, org_id),
    )
    assert remove.status_code == 204, getattr(remove, "text", "")[:300]

    # Now every call returns 403
    after = test_client.get(
        "/api/data_sources", headers=_h(victim["token"], org_id)
    )
    assert after.status_code == 403, (
        f"After removal, expected 403, got {after.status_code}: "
        f"{getattr(after, 'text', '')[:300]}"
    )
    assert "not a member" in after.json().get("detail", "").lower(), (
        f"Expected 'not a member' detail, got: {after.json()}"
    )
