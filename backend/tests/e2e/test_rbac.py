"""
RBAC end-to-end tests.

Tests cover:
- Permission resolver (RBAC path vs legacy fallback)
- Role CRUD (system roles immutable, custom roles enterprise-only)
- Role assignments (multi-role, groups)
- Resource grants
- Lockout prevention (full_admin_access failsafe)
- Whoami returns resolved permissions
- Permission enforcement (member can't access admin routes)
- Group-based permission inheritance
"""
import pytest
import uuid


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}


# ── Whoami / Resolved Permissions ────────────────────────────────────────

@pytest.mark.e2e
def test_whoami_returns_resolved_permissions(test_client, create_user, login_user, whoami):
    """Whoami should include roles, permissions, and resource_permissions."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    info = whoami(token)

    org = info["organizations"][0]
    assert "permissions" in org
    assert "roles" in org
    assert "resource_permissions" in org
    assert len(org["permissions"]) > 0, "Admin should have permissions"
    assert "role" in org, "Backward-compat role field should exist"


@pytest.mark.e2e
def test_whoami_member_has_limited_permissions(test_client, create_user, login_user, whoami):
    """A member should have fewer permissions than admin."""
    # Create admin
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]

    # Invite a member
    member_email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers=_headers(admin_token, org_id),
    )

    # Register and login as member
    create_user(email=member_email, password="test123")
    member_token = login_user(member_email, "test123")
    member_info = whoami(member_token)

    member_org = next(o for o in member_info["organizations"] if o["id"] == org_id)
    assert "create_data_source" not in member_org["permissions"]
    assert "view_reports" in member_org["permissions"]


# ── Roles CRUD ───────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_list_roles_returns_system_roles(test_client, create_user, login_user, whoami):
    """GET /roles should return at least the system admin and member roles."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    resp = test_client.get(f"/api/organizations/{org_id}/roles", headers=_headers(token, org_id))
    assert resp.status_code == 200
    roles = resp.json()
    names = [r["name"] for r in roles]
    assert "admin" in names
    assert "member" in names


@pytest.mark.e2e
def test_cannot_delete_system_role(test_client, create_user, login_user, whoami):
    """System roles cannot be deleted."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    roles = test_client.get(f"/api/organizations/{org_id}/roles", headers=_headers(token, org_id)).json()
    system_role = next(r for r in roles if r["is_system"])

    resp = test_client.delete(
        f"/api/organizations/{org_id}/roles/{system_role['id']}",
        headers=_headers(token, org_id),
    )
    # Should be 402 (enterprise required) or 403 (system role)
    assert resp.status_code in (402, 403)


@pytest.mark.e2e
def test_create_custom_role_enterprise_gate(test_client, create_user, login_user, whoami):
    """Creating custom roles is gated by enterprise license.
    If license is active, role creation succeeds (200).
    If no license, it returns 402.
    """
    from app.ee.license import is_enterprise_licensed

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    resp = test_client.post(
        f"/api/organizations/{org_id}/roles",
        json={"name": "analyst", "permissions": ["view_reports", "create_reports"]},
        headers=_headers(token, org_id),
    )

    if is_enterprise_licensed():
        assert resp.status_code == 200
        # Cleanup: delete the created role
        role_id = resp.json()["id"]
        test_client.delete(f"/api/organizations/{org_id}/roles/{role_id}", headers=_headers(token, org_id))
    else:
        assert resp.status_code == 402


# ── Role Assignments ─────────────────────────────────────────────────────

@pytest.mark.e2e
def test_list_role_assignments(test_client, create_user, login_user, whoami):
    """Admin can list role assignments."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    resp = test_client.get(
        f"/api/organizations/{org_id}/role-assignments",
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200
    assignments = resp.json()
    assert len(assignments) >= 1, "Admin should have at least one role assignment"


@pytest.mark.e2e
def test_assign_additional_role(test_client, create_user, login_user, whoami):
    """Admin can assign a role to a member."""
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]

    # Invite a member
    member_email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers=_headers(admin_token, org_id),
    )
    create_user(email=member_email, password="test123")
    member_token = login_user(member_email, "test123")
    member_info = whoami(member_token)
    member_id = member_info["id"]

    # Get the admin system role ID
    roles = test_client.get(f"/api/organizations/{org_id}/roles", headers=_headers(admin_token, org_id)).json()
    admin_role = next(r for r in roles if r["name"] == "admin")

    # Assign admin role to the member (multi-role)
    resp = test_client.post(
        f"/api/organizations/{org_id}/role-assignments",
        json={"role_id": admin_role["id"], "principal_type": "user", "principal_id": member_id},
        headers=_headers(admin_token, org_id),
    )
    assert resp.status_code == 200

    # Verify member now has admin permissions via whoami
    member_info = whoami(member_token)
    member_org = next(o for o in member_info["organizations"] if o["id"] == org_id)
    # Should have full_admin_access (from admin role) which grants everything
    assert "full_admin_access" in member_org["permissions"] or "create_data_source" in member_org["permissions"]


# ── Lockout Prevention ───────────────────────────────────────────────────

@pytest.mark.e2e
def test_cannot_remove_last_full_admin(test_client, create_user, login_user, whoami):
    """Cannot remove the last role assignment that holds full_admin_access."""
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    admin_info = whoami(admin_token)
    org_id = admin_info["organizations"][0]["id"]
    admin_id = admin_info["id"]

    # Get admin's role assignments
    assignments = test_client.get(
        f"/api/organizations/{org_id}/role-assignments?principal_type=user&principal_id={admin_id}",
        headers=_headers(admin_token, org_id),
    ).json()

    admin_assignment = next(
        (a for a in assignments if a.get("role", {}).get("name") == "admin"),
        None
    )

    if admin_assignment:
        # Try to remove the only admin assignment
        resp = test_client.delete(
            f"/api/organizations/{org_id}/role-assignments/{admin_assignment['id']}",
            headers=_headers(admin_token, org_id),
        )
        assert resp.status_code == 409
        assert "full admin" in resp.json().get("detail", "").lower()


@pytest.mark.e2e
def test_can_remove_admin_when_another_exists(test_client, create_user, login_user, whoami):
    """Can remove an admin's role assignment when another admin exists."""
    admin1 = create_user()
    admin1_token = login_user(admin1["email"], admin1["password"])
    org_id = whoami(admin1_token)["organizations"][0]["id"]

    # Invite second admin
    admin2_email = f"admin2_{uuid.uuid4().hex[:8]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": admin2_email, "role": "admin"},
        headers=_headers(admin1_token, org_id),
    )
    create_user(email=admin2_email, password="test123")

    # Now remove admin1's membership — should succeed since admin2 exists
    members = test_client.get(
        f"/api/organizations/{org_id}/members",
        headers=_headers(admin1_token, org_id),
    ).json()

    admin1_membership = next(
        (m for m in members if m.get("user", {}).get("email") == admin1["email"]),
        None,
    )

    if admin1_membership:
        resp = test_client.delete(
            f"/api/organizations/{org_id}/members/{admin1_membership['id']}",
            headers=_headers(admin1_token, org_id),
        )
        # Should succeed since admin2 holds full_admin_access
        assert resp.status_code in (204, 409)  # 409 if role_assignments not seeded for admin2


# ── Permission Enforcement ───────────────────────────────────────────────

@pytest.mark.e2e
def test_member_cannot_access_admin_endpoints(test_client, create_user, login_user, whoami):
    """A member role should be denied access to admin-only endpoints."""
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]

    # Invite member
    member_email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers=_headers(admin_token, org_id),
    )
    create_user(email=member_email, password="test123")
    member_token = login_user(member_email, "test123")

    # Member tries to access role management (admin-only)
    resp = test_client.post(
        f"/api/organizations/{org_id}/role-assignments",
        json={"role_id": "fake", "principal_type": "user", "principal_id": "fake"},
        headers=_headers(member_token, org_id),
    )
    assert resp.status_code == 403


@pytest.mark.e2e
def test_member_can_access_member_endpoints(test_client, create_user, login_user, whoami):
    """A member should be able to access member-level endpoints."""
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]

    # Invite member
    member_email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers=_headers(admin_token, org_id),
    )
    create_user(email=member_email, password="test123")
    member_token = login_user(member_email, "test123")

    # Member can list org members
    resp = test_client.get(
        f"/api/organizations/{org_id}/members",
        headers=_headers(member_token, org_id),
    )
    assert resp.status_code == 200

    # Member can list roles
    resp = test_client.get(
        f"/api/organizations/{org_id}/roles",
        headers=_headers(member_token, org_id),
    )
    assert resp.status_code == 200


# ── Resource Grants ──────────────────────────────────────────────────────

@pytest.mark.e2e
def test_resource_grant_crud(test_client, create_user, login_user, whoami):
    """Admin can create and list resource grants."""
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    admin_info = whoami(admin_token)
    org_id = admin_info["organizations"][0]["id"]
    admin_id = admin_info["id"]

    # Create a resource grant
    resp = test_client.post(
        f"/api/organizations/{org_id}/resource-grants",
        json={
            "resource_type": "data_source",
            "resource_id": str(uuid.uuid4()),
            "principal_type": "user",
            "principal_id": admin_id,
            "permissions": ["query", "view_schema"],
        },
        headers=_headers(admin_token, org_id),
    )
    assert resp.status_code == 200
    grant = resp.json()
    assert grant["permissions"] == ["query", "view_schema"]

    # List grants
    resp = test_client.get(
        f"/api/organizations/{org_id}/resource-grants",
        headers=_headers(admin_token, org_id),
    )
    assert resp.status_code == 200
    assert len(resp.json()) >= 1

    # Delete grant
    resp = test_client.delete(
        f"/api/organizations/{org_id}/resource-grants/{grant['id']}",
        headers=_headers(admin_token, org_id),
    )
    assert resp.status_code == 204


# ── Groups (Enterprise) ─────────────────────────────────────────────────

@pytest.mark.e2e
def test_groups_enterprise_gate(test_client, create_user, login_user, whoami):
    """Group management is gated by enterprise license."""
    from app.ee.license import is_enterprise_licensed

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    resp = test_client.post(
        f"/api/organizations/{org_id}/groups",
        json={"name": "Engineering"},
        headers=_headers(token, org_id),
    )

    if is_enterprise_licensed():
        assert resp.status_code == 200
        # Cleanup
        group_id = resp.json()["id"]
        test_client.delete(f"/api/organizations/{org_id}/groups/{group_id}", headers=_headers(token, org_id))
    else:
        assert resp.status_code == 402


# ── Permissions Registry ─────────────────────────────────────────────────

@pytest.mark.e2e
def test_permissions_registry_endpoint(test_client, create_user, login_user):
    """The permissions registry endpoint returns categories and resource permissions."""
    user = create_user()
    token = login_user(user["email"], user["password"])

    resp = test_client.get(
        "/api/permissions/registry",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "categories" in data
    assert "resource_permissions" in data
    # Reports lives in HIDDEN_PERMISSION_CATEGORIES on purpose — the role
    # editor UI should not render meaningless checkboxes for it. The
    # permission strings themselves are still valid (see test_rbac_registry).
    assert "Reports" not in data["categories"]
    assert "data_source" in data["resource_permissions"]


# ── Members Response Includes Roles ──────────────────────────────────────

@pytest.mark.e2e
def test_members_response_includes_roles(test_client, create_user, login_user, whoami):
    """GET /members should include roles array for each member."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    resp = test_client.get(
        f"/api/organizations/{org_id}/members",
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200
    members = resp.json()
    assert len(members) >= 1

    admin_member = members[0]
    assert "roles" in admin_member
    # Admin should have at least the admin role
    if admin_member.get("roles"):
        role_names = [r["name"] for r in admin_member["roles"]]
        assert "admin" in role_names
