"""
RBAC end-to-end coverage for the resolver paths in
``permission_resolver._resolve_permissions_inner``.

Walks every path that produces effective permissions for a user:

  1. Direct user role assignment (system 'admin' role → full_admin_access)
  2. Direct user role assignment (custom role with explicit perms list)
  3. Group → role assignment (user inherits via group membership)
  4. Direct user resource-grant (per-DS access)
  5. Group resource-grant (per-DS access via group)
  6. Role-as-principal resource grant (resource_grant.principal_type='role';
     the user inherits the grant transitively through any role they
     hold via direct or group assignment)
  7. full_admin_access wildcard bypass for resource permissions
  8. ``view`` / ``view_schema`` implicit on any grant
  9. Mutation freshness — newly-assigned permissions take effect on next request

The tests assert behaviour through the public API only — they read
``GET /users/whoami`` to inspect the resolver's output.

Group + custom-role tests are gated by enterprise license; the
``enterprise_license`` monkey-patch fixture activates a fake license
for the duration of those tests.
"""
import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _whoami_org(whoami, token, org_id):
    info = whoami(token)
    return next(o for o in info["organizations"] if o["id"] == org_id)


# ────────────────────────────────────────────────────────────────────
# Path 1 + 6 — system admin role + full_admin_access bypass
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_admin_full_admin_access_resolves(test_client, bootstrap_admin, whoami):
    """The system admin role should resolve to full_admin_access wildcard."""
    admin = bootstrap_admin()
    org = _whoami_org(whoami, admin["token"], admin["org_id"])
    assert "full_admin_access" in org["permissions"], (
        f"admin missing full_admin_access; got {sorted(org['permissions'])}"
    )
    assert "admin" in org["roles"]


# ────────────────────────────────────────────────────────────────────
# Path 1 — direct user system-role assignment
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_member_role_resolves_to_default_member_perms(
    test_client, bootstrap_admin, invite_user_to_org, whoami
):
    """An invited member gets the default member permission set, not admin."""
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])

    org = _whoami_org(whoami, member["token"], admin["org_id"])
    perms = set(org["permissions"])

    assert "full_admin_access" not in perms
    # Default member seed contains the hidden Reports perms + manage_files + view_members
    assert {"view_reports", "create_reports", "manage_files", "view_members"} <= perms
    assert "create_data_source" not in perms
    assert "manage_settings" not in perms
    assert "member" in org["roles"]


# ────────────────────────────────────────────────────────────────────
# Path 2 + 7 — assign an additional role and observe the effect
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_additional_role_assignment_takes_effect(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    get_system_role,
    assign_role,
    whoami,
):
    """Assigning the system admin role to a member should immediately grant full_admin_access.

    Verifies the resolver does not stale-cache role lookups across
    requests — a single subsequent /whoami sees the new permissions.
    """
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])

    # Sanity: pre-assignment, member doesn't have full_admin_access.
    org_before = _whoami_org(whoami, member["token"], admin["org_id"])
    assert "full_admin_access" not in set(org_before["permissions"])

    # Assign the system admin role to them.
    admin_role = get_system_role("admin", user_token=admin["token"], org_id=admin["org_id"])
    assign_resp = assign_role(
        role_id=admin_role["id"],
        principal_type="user",
        principal_id=member["user_id"],
        user_token=admin["token"],
        org_id=admin["org_id"],
    )
    assert assign_resp.status_code == 200, assign_resp.text

    # Resolver returns up-to-date permissions on the very next call.
    org_after = _whoami_org(whoami, member["token"], admin["org_id"])
    assert "full_admin_access" in set(org_after["permissions"])
    assert {"admin", "member"} <= set(org_after["roles"])


# ────────────────────────────────────────────────────────────────────
# Path 4 — per-DS resource grant for a user
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_user_resource_grant_appears_in_whoami(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    sqlite_data_source,
    grant_resource,
    whoami,
):
    """A per-DS resource grant must surface in whoami['resource_permissions']."""
    admin = bootstrap_admin()
    org_id = admin["org_id"]
    ds = sqlite_data_source(name="rp_ds", user_token=admin["token"], org_id=org_id)
    test_client.put(
        f"/api/data_sources/{ds['id']}",
        json={"is_public": False},
        headers=_hdr(admin["token"], org_id),
    )

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    grant_resp = grant_resource(
        resource_type="data_source",
        resource_id=ds["id"],
        principal_type="user",
        principal_id=member["user_id"],
        permissions=["manage_instructions"],
        user_token=admin["token"],
        org_id=org_id,
    )
    assert grant_resp.status_code == 200, grant_resp.json()

    org = _whoami_org(whoami, member["token"], org_id)
    rp = org["resource_permissions"]
    key = f"data_source:{ds['id']}"
    assert key in rp, f"resource_permissions missing {key}: {rp}"
    assert "manage_instructions" in set(rp[key])

    # And the member can open the DS detail — view is implicit on any grant.
    detail = test_client.get(
        f"/api/data_sources/{ds['id']}",
        headers=_hdr(member["token"], org_id),
    )
    assert detail.status_code == 200, detail.text


# ────────────────────────────────────────────────────────────────────
# Path 3 + 5 — group → role assignment + group resource grant
# Both gated by enterprise license stub.
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_group_role_assignment_grants_member_permissions(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    enterprise_license,
    create_group,
    add_user_to_group,
    create_role,
    assign_role,
    whoami,
):
    """A user added to a group should inherit the role assigned to that group."""
    admin = bootstrap_admin()
    org_id = admin["org_id"]

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    grp_resp = create_group(name="data-team", user_token=admin["token"], org_id=org_id)
    if grp_resp.status_code == 402:
        pytest.skip("enterprise license stub did not take effect for groups")
    assert grp_resp.status_code == 200, grp_resp.text
    group = grp_resp.json()

    add_resp = add_user_to_group(
        group_id=group["id"],
        user_id=member["user_id"],
        user_token=admin["token"],
        org_id=org_id,
    )
    assert add_resp.status_code in (200, 201), add_resp.text

    role_resp = create_role(
        name="connection-mgr",
        permissions=["manage_connections", "view_members"],
        user_token=admin["token"],
        org_id=org_id,
    )
    if role_resp.status_code == 402:
        pytest.skip("enterprise license stub did not take effect for custom roles")
    assert role_resp.status_code == 200, role_resp.text
    role = role_resp.json()

    assign = assign_role(
        role_id=role["id"],
        principal_type="group",
        principal_id=group["id"],
        user_token=admin["token"],
        org_id=org_id,
    )
    assert assign.status_code == 200, assign.text

    org = _whoami_org(whoami, member["token"], org_id)
    perms = set(org["permissions"])
    assert "manage_connections" in perms
    assert "view_members" in perms
    # Member's seeded role perms should still be present.
    assert "view_reports" in perms


@pytest.mark.e2e
def test_group_resource_grant_grants_per_ds_access(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    sqlite_data_source,
    enterprise_license,
    create_group,
    add_user_to_group,
    grant_resource,
    whoami,
):
    """A resource grant whose principal is a group should grant access to all members."""
    admin = bootstrap_admin()
    org_id = admin["org_id"]

    ds = sqlite_data_source(name="grp_ds", user_token=admin["token"], org_id=org_id)
    test_client.put(
        f"/api/data_sources/{ds['id']}",
        json={"is_public": False},
        headers=_hdr(admin["token"], org_id),
    )

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    grp_resp = create_group(name="ds-readers", user_token=admin["token"], org_id=org_id)
    if grp_resp.status_code == 402:
        pytest.skip("enterprise license stub did not take effect for groups")
    assert grp_resp.status_code == 200, grp_resp.text
    group = grp_resp.json()

    add_resp = add_user_to_group(
        group_id=group["id"],
        user_id=member["user_id"],
        user_token=admin["token"],
        org_id=org_id,
    )
    assert add_resp.status_code in (200, 201), add_resp.text

    grant_resp = grant_resource(
        resource_type="data_source",
        resource_id=ds["id"],
        principal_type="group",
        principal_id=group["id"],
        permissions=["manage_instructions"],
        user_token=admin["token"],
        org_id=org_id,
    )
    assert grant_resp.status_code == 200, grant_resp.json()

    org = _whoami_org(whoami, member["token"], org_id)
    rp = org["resource_permissions"]
    key = f"data_source:{ds['id']}"
    assert key in rp, f"resource_permissions missing {key}: {rp}"
    assert "manage_instructions" in set(rp[key])

    # And the user can now open the DS detail endpoint.
    detail = test_client.get(
        f"/api/data_sources/{ds['id']}",
        headers=_hdr(member["token"], org_id),
    )
    assert detail.status_code == 200, detail.text


# ────────────────────────────────────────────────────────────────────
# Negative — role assignment removed before mutation request
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_role_assignment_removal_takes_effect(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    list_role_assignments,
    get_system_role,
    assign_role,
    whoami,
):
    """Removing a role assignment should immediately drop the inherited perms."""
    admin = bootstrap_admin()
    org_id = admin["org_id"]
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    admin_role = get_system_role("admin", user_token=admin["token"], org_id=org_id)
    assign_resp = assign_role(
        role_id=admin_role["id"],
        principal_type="user",
        principal_id=member["user_id"],
        user_token=admin["token"],
        org_id=org_id,
    )
    assert assign_resp.status_code == 200
    assignment_id = assign_resp.json()["id"]

    # Confirm the assignment took effect
    assert "full_admin_access" in set(_whoami_org(whoami, member["token"], org_id)["permissions"])

    # Remove the admin role assignment via the API
    delete_resp = test_client.delete(
        f"/api/organizations/{org_id}/role-assignments/{assignment_id}",
        headers=_hdr(admin["token"], org_id),
    )
    assert delete_resp.status_code == 204, delete_resp.text

    # Member should drop back to base member permissions immediately.
    after = _whoami_org(whoami, member["token"], org_id)
    assert "full_admin_access" not in set(after["permissions"])
    assert "admin" not in after["roles"]


# ────────────────────────────────────────────────────────────────────
# Path 6 — role-as-principal resource grant
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_role_as_principal_resource_grant(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    sqlite_data_source,
    enterprise_license,
    create_role,
    assign_role,
    whoami,
):
    """A resource_grant whose principal is a role should propagate to every
    user assigned that role.

    This is the path the resolver started supporting in the latest
    ``rbac improvements`` commit: when computing a user's resource
    permissions, the resolver now also queries ResourceGrant rows whose
    ``principal_type='role'`` and ``principal_id`` is in the user's
    role IDs (direct or via groups).

    We exercise this end-to-end by:
      1. Creating a custom role that carries an inline ``resource_grants``
         payload (the route persists this via rbac_service._sync_role_grants)
      2. Assigning the role to a member directly
      3. Asserting whoami exposes the per-DS permission AND the member
         can open the DS detail (view is implicit on any grant)
    """
    admin = bootstrap_admin()
    org_id = admin["org_id"]

    ds = sqlite_data_source(name="role_grant_ds", user_token=admin["token"], org_id=org_id)
    test_client.put(
        f"/api/data_sources/{ds['id']}",
        json={"is_public": False},
        headers=_hdr(admin["token"], org_id),
    )

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    # Create the role WITH an inline resource grant. The /roles route
    # accepts ``resource_grants`` and persists them via rbac_service.
    role_payload = {
        "name": "ds-instructions-mgr",
        "permissions": ["view_members"],
        "resource_grants": [
            {
                "resource_type": "data_source",
                "resource_id": ds["id"],
                "permissions": ["manage_instructions"],
            }
        ],
    }
    create_resp = test_client.post(
        f"/api/organizations/{org_id}/roles",
        json=role_payload,
        headers=_hdr(admin["token"], org_id),
    )
    if create_resp.status_code == 402:
        pytest.skip("enterprise license stub did not take effect for custom roles")
    assert create_resp.status_code == 200, create_resp.text
    role = create_resp.json()
    assert role["resource_grants"], f"role response missing resource_grants: {role}"

    # Assign that role to the member directly
    assign = assign_role(
        role_id=role["id"],
        principal_type="user",
        principal_id=member["user_id"],
        user_token=admin["token"],
        org_id=org_id,
    )
    assert assign.status_code == 200, assign.text

    # Resolver must surface the role-attached grant on the member's whoami
    org = _whoami_org(whoami, member["token"], org_id)
    rp = org["resource_permissions"]
    key = f"data_source:{ds['id']}"
    assert key in rp, f"role-as-principal grant missing from whoami: {rp}"
    assert "manage_instructions" in set(rp[key])

    # And the member can open the DS detail — view is implicit on the grant.
    detail = test_client.get(
        f"/api/data_sources/{ds['id']}",
        headers=_hdr(member["token"], org_id),
    )
    assert detail.status_code == 200, detail.text


@pytest.mark.e2e
def test_role_as_principal_grant_via_group_assignment(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    sqlite_data_source,
    enterprise_license,
    create_role,
    create_group,
    add_user_to_group,
    assign_role,
    whoami,
):
    """A user inherits a role-attached grant via group → role assignment.

    Resolver chain: user → group_membership → role_assignment(group→role)
    → role_id → resource_grant(principal_type=role, principal_id=role_id).
    """
    admin = bootstrap_admin()
    org_id = admin["org_id"]

    ds = sqlite_data_source(name="grp_role_ds", user_token=admin["token"], org_id=org_id)
    test_client.put(
        f"/api/data_sources/{ds['id']}",
        json={"is_public": False},
        headers=_hdr(admin["token"], org_id),
    )

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    grp_resp = create_group(name="instr-team", user_token=admin["token"], org_id=org_id)
    if grp_resp.status_code == 402:
        pytest.skip("enterprise license stub did not take effect for groups")
    assert grp_resp.status_code == 200, grp_resp.text
    group = grp_resp.json()

    add_resp = add_user_to_group(
        group_id=group["id"],
        user_id=member["user_id"],
        user_token=admin["token"],
        org_id=org_id,
    )
    assert add_resp.status_code in (200, 201), add_resp.text

    role_payload = {
        "name": "team-instructions-mgr",
        "permissions": [],
        "resource_grants": [
            {
                "resource_type": "data_source",
                "resource_id": ds["id"],
                "permissions": ["manage_instructions"],
            }
        ],
    }
    create_resp = test_client.post(
        f"/api/organizations/{org_id}/roles",
        json=role_payload,
        headers=_hdr(admin["token"], org_id),
    )
    if create_resp.status_code == 402:
        pytest.skip("enterprise license stub did not take effect for custom roles")
    assert create_resp.status_code == 200, create_resp.text
    role = create_resp.json()

    assign = assign_role(
        role_id=role["id"],
        principal_type="group",
        principal_id=group["id"],
        user_token=admin["token"],
        org_id=org_id,
    )
    assert assign.status_code == 200, assign.text

    org = _whoami_org(whoami, member["token"], org_id)
    rp = org["resource_permissions"]
    key = f"data_source:{ds['id']}"
    assert key in rp, f"transitive role-as-principal grant missing from whoami: {rp}"
    assert "manage_instructions" in set(rp[key])


# ────────────────────────────────────────────────────────────────────
# Path 8 — view / view_schema implicit on any grant
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_view_and_view_schema_are_implicit_on_any_grant(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    sqlite_data_source,
    grant_resource,
):
    """Holding any non-empty grant on a DS should imply view + view_schema.

    The resolver special-cases ``view`` and ``view_schema`` for data
    sources: if the user has any entry in ``resource_permissions`` for
    that DS, those two perms return True automatically. They are no
    longer surfaced as explicit checkboxes in the registry.

    We grant ONLY ``manage_instructions`` (no view, no view_schema) and
    assert the holder can:
      - GET /data_sources/{id}    (route requires 'view')
      - GET /data_sources/{id}/full_schema  (route requires 'view_schema')
    """
    admin = bootstrap_admin()
    org_id = admin["org_id"]

    ds = sqlite_data_source(name="implicit_view_ds", user_token=admin["token"], org_id=org_id)
    test_client.put(
        f"/api/data_sources/{ds['id']}",
        json={"is_public": False},
        headers=_hdr(admin["token"], org_id),
    )

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    grant_resp = grant_resource(
        resource_type="data_source",
        resource_id=ds["id"],
        principal_type="user",
        principal_id=member["user_id"],
        permissions=["manage_instructions"],
        user_token=admin["token"],
        org_id=org_id,
    )
    assert grant_resp.status_code == 200, grant_resp.json()

    # GET /data_sources/{id} — requires_resource_permission('data_source', 'view')
    detail = test_client.get(
        f"/api/data_sources/{ds['id']}",
        headers=_hdr(member["token"], org_id),
    )
    assert detail.status_code == 200, detail.text

    # GET /data_sources/{id}/full_schema — requires_resource_permission('data_source', 'view_schema')
    schema = test_client.get(
        f"/api/data_sources/{ds['id']}/full_schema",
        headers=_hdr(member["token"], org_id),
    )
    assert schema.status_code == 200, schema.text
