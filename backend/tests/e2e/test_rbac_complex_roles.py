"""
Complex RBAC role tests — validates permission resolution and enforcement
for realistic multi-role, multi-resource scenarios.

Test roles:
1. Analyst: query specific DS, view everything org-wide, can't create/manage
2. Instruction Author: create instructions on specific DS only
3. Eval Runner: run evals on specific DS, view org-wide
4. Data Source Admin: full access on specific DS, no org-wide admin

Tests cover:
- Permission resolution (resolve_permissions returns correct sets)
- Two-tier OR logic (org-level wildcard vs resource-scoped)
- Role stacking (user with multiple roles → union of permissions)
- Group-based inheritance
- Negative enforcement (denied when missing both org and resource permission)
"""
import pytest
import uuid


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}


def _setup_org_with_member(test_client, create_user, login_user, whoami):
    """Create admin + member in an org, return context dict."""
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    admin_info = whoami(admin_token)
    org_id = admin_info["organizations"][0]["id"]
    admin_id = admin_info["id"]

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

    return {
        "org_id": org_id,
        "admin_token": admin_token,
        "admin_id": admin_id,
        "member_token": member_token,
        "member_id": member_id,
        "member_email": member_email,
    }


def _create_custom_role(test_client, admin_token, org_id, name, permissions):
    """Create a custom role. Returns role dict or None if enterprise not available."""
    resp = test_client.post(
        f"/api/organizations/{org_id}/roles",
        json={"name": name, "permissions": permissions},
        headers=_headers(admin_token, org_id),
    )
    if resp.status_code == 402:
        return None  # Enterprise not available
    assert resp.status_code == 200, f"Failed to create role '{name}': {resp.text}"
    return resp.json()


def _assign_role(test_client, admin_token, org_id, role_id, principal_type, principal_id):
    """Assign a role to a user or group."""
    resp = test_client.post(
        f"/api/organizations/{org_id}/role-assignments",
        json={"role_id": role_id, "principal_type": principal_type, "principal_id": principal_id},
        headers=_headers(admin_token, org_id),
    )
    assert resp.status_code == 200, f"Failed to assign role: {resp.text}"
    return resp.json()


def _grant_resource(test_client, admin_token, org_id, resource_type, resource_id, principal_type, principal_id, permissions):
    """Create a resource grant."""
    resp = test_client.post(
        f"/api/organizations/{org_id}/resource-grants",
        json={
            "resource_type": resource_type,
            "resource_id": resource_id,
            "principal_type": principal_type,
            "principal_id": principal_id,
            "permissions": permissions,
        },
        headers=_headers(admin_token, org_id),
    )
    assert resp.status_code == 200, f"Failed to create resource grant: {resp.text}"
    return resp.json()


def _get_whoami_perms(whoami, token, org_id):
    """Get resolved permissions and resource_permissions from whoami."""
    info = whoami(token)
    org = next(o for o in info["organizations"] if o["id"] == org_id)
    return {
        "permissions": set(org.get("permissions", [])),
        "resource_permissions": org.get("resource_permissions", {}),
        "roles": org.get("roles", []),
    }


def _requires_enterprise(test_client, admin_token, org_id):
    """Check if enterprise features are available by trying to create a role."""
    resp = test_client.post(
        f"/api/organizations/{org_id}/roles",
        json={"name": f"_probe_{uuid.uuid4().hex[:6]}", "permissions": ["view_reports"]},
        headers=_headers(admin_token, org_id),
    )
    if resp.status_code == 200:
        # Cleanup probe role
        role_id = resp.json()["id"]
        test_client.delete(
            f"/api/organizations/{org_id}/roles/{role_id}",
            headers=_headers(admin_token, org_id),
        )
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
# 1. Analyst Role — view org-wide, query specific DS
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
def test_analyst_role_resolution(test_client, create_user, login_user, whoami, dynamic_sqlite_db, create_data_source):
    """Analyst: org-level view perms + resource-level query on specific DS."""
    ctx = _setup_org_with_member(test_client, create_user, login_user, whoami)

    if not _requires_enterprise(test_client, ctx["admin_token"], ctx["org_id"]):
        pytest.skip("Enterprise license required for custom roles")

    # Create a data source
    ds = create_data_source(
        name="analyst-test-ds",
        type="sqlite",
        config={"database": dynamic_sqlite_db},
        credentials={},
        user_token=ctx["admin_token"],
        org_id=ctx["org_id"],
    )
    ds_id = ds["id"]

    # Create analyst role
    analyst_role = _create_custom_role(test_client, ctx["admin_token"], ctx["org_id"], "Analyst", [
        "view_reports", "view_instructions", "view_entities", "view_evals", "export_query",
    ])

    # Assign to member
    _assign_role(test_client, ctx["admin_token"], ctx["org_id"], analyst_role["id"], "user", ctx["member_id"])

    # Grant query access on the specific DS
    _grant_resource(
        test_client, ctx["admin_token"], ctx["org_id"],
        "data_source", ds_id, "user", ctx["member_id"],
        ["query", "view_schema"],
    )

    # Check resolved permissions
    perms = _get_whoami_perms(whoami, ctx["member_token"], ctx["org_id"])

    # Should have org-level view permissions
    assert "view_reports" in perms["permissions"]
    assert "view_instructions" in perms["permissions"]
    assert "view_entities" in perms["permissions"]
    assert "view_evals" in perms["permissions"]
    assert "export_query" in perms["permissions"]

    # Should NOT have create/manage permissions
    assert "create_instructions" not in perms["permissions"]
    assert "create_entities" not in perms["permissions"]
    assert "manage_evals" not in perms["permissions"]

    # Should have resource-level query on the DS
    ds_key = f"data_source:{ds_id}"
    assert ds_key in perms["resource_permissions"]
    ds_perms = set(perms["resource_permissions"][ds_key])
    assert "query" in ds_perms
    assert "view_schema" in ds_perms


@pytest.mark.e2e
def test_analyst_can_view_evals_but_not_manage(test_client, create_user, login_user, whoami, dynamic_sqlite_db, create_data_source):
    """Analyst can list eval suites (view_evals) but cannot create them (manage_evals)."""
    ctx = _setup_org_with_member(test_client, create_user, login_user, whoami)

    if not _requires_enterprise(test_client, ctx["admin_token"], ctx["org_id"]):
        pytest.skip("Enterprise license required for custom roles")

    analyst_role = _create_custom_role(test_client, ctx["admin_token"], ctx["org_id"], "Analyst-View", [
        "view_reports", "view_evals",
    ])
    _assign_role(test_client, ctx["admin_token"], ctx["org_id"], analyst_role["id"], "user", ctx["member_id"])

    # Can list suites (view_evals)
    resp = test_client.get(
        "/api/tests/suites",
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code == 200

    # Cannot create suite (manage_evals)
    resp = test_client.post(
        "/api/tests/suites",
        json={"name": "Unauthorized"},
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# 2. Instruction Author — create instructions on specific DS only
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
def test_instruction_author_scoped_to_ds(test_client, create_user, login_user, whoami, dynamic_sqlite_db, create_data_source):
    """Instruction Author can create instructions on granted DS but denied on others."""
    ctx = _setup_org_with_member(test_client, create_user, login_user, whoami)

    if not _requires_enterprise(test_client, ctx["admin_token"], ctx["org_id"]):
        pytest.skip("Enterprise license required for custom roles")

    # Create two data sources
    ds_granted = create_data_source(
        name="author-granted-ds",
        type="sqlite",
        config={"database": dynamic_sqlite_db},
        credentials={},
        user_token=ctx["admin_token"],
        org_id=ctx["org_id"],
    )
    ds_denied_id = str(uuid.uuid4())  # Fake DS — no grant exists

    # Create role with view-level org permissions (no org-level create_instructions)
    author_role = _create_custom_role(test_client, ctx["admin_token"], ctx["org_id"], "Instruction Author", [
        "view_reports", "view_instructions", "view_entities", "view_evals",
    ])
    _assign_role(test_client, ctx["admin_token"], ctx["org_id"], author_role["id"], "user", ctx["member_id"])

    # Grant create_instructions on the specific DS
    _grant_resource(
        test_client, ctx["admin_token"], ctx["org_id"],
        "data_source", ds_granted["id"], "user", ctx["member_id"],
        ["query", "view_schema", "view_instructions", "create_instructions"],
    )

    # Author creates instruction on granted DS — should succeed
    resp = test_client.post(
        "/api/instructions",
        json={
            "text": "Instruction on granted DS",
            "status": "draft",
            "data_source_ids": [ds_granted["id"]],
        },
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code == 200, f"Should succeed on granted DS: {resp.text}"

    # Author creates instruction on non-granted DS — should be denied
    resp = test_client.post(
        "/api/instructions",
        json={
            "text": "Instruction on denied DS",
            "status": "draft",
            "data_source_ids": [ds_denied_id],
        },
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code == 403, "Should be denied on non-granted DS"


@pytest.mark.e2e
def test_org_level_create_instructions_bypasses_resource_check(test_client, create_user, login_user, whoami, dynamic_sqlite_db, create_data_source):
    """User with org-level create_instructions can create on ANY DS (two-tier OR)."""
    ctx = _setup_org_with_member(test_client, create_user, login_user, whoami)

    if not _requires_enterprise(test_client, ctx["admin_token"], ctx["org_id"]):
        pytest.skip("Enterprise license required for custom roles")

    ds = create_data_source(
        name="or-test-ds",
        type="sqlite",
        config={"database": dynamic_sqlite_db},
        credentials={},
        user_token=ctx["admin_token"],
        org_id=ctx["org_id"],
    )

    # Role with org-level create_instructions (wildcard for all DS)
    wildcard_role = _create_custom_role(test_client, ctx["admin_token"], ctx["org_id"], "Instruction Wildcard", [
        "view_reports", "view_instructions", "create_instructions",
    ])
    _assign_role(test_client, ctx["admin_token"], ctx["org_id"], wildcard_role["id"], "user", ctx["member_id"])

    # No resource grant on this DS — but org-level permission should suffice
    resp = test_client.post(
        "/api/instructions",
        json={
            "text": "Instruction via org-level perm",
            "status": "draft",
            "data_source_ids": [ds["id"]],
        },
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code == 200, f"Org-level perm should bypass resource check: {resp.text}"


# ═══════════════════════════════════════════════════════════════════════════
# 3. Eval Runner — run evals on specific DS
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
def test_eval_runner_can_run_but_not_manage(test_client, create_user, login_user, whoami):
    """Eval Runner with run_evals can trigger runs but cannot create suites/cases."""
    ctx = _setup_org_with_member(test_client, create_user, login_user, whoami)

    if not _requires_enterprise(test_client, ctx["admin_token"], ctx["org_id"]):
        pytest.skip("Enterprise license required for custom roles")

    runner_role = _create_custom_role(test_client, ctx["admin_token"], ctx["org_id"], "Eval Runner", [
        "view_reports", "view_evals", "run_evals",
    ])
    _assign_role(test_client, ctx["admin_token"], ctx["org_id"], runner_role["id"], "user", ctx["member_id"])

    # Can list suites (view_evals)
    resp = test_client.get(
        "/api/tests/suites",
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code == 200

    # Cannot create suite (needs manage_evals)
    resp = test_client.post(
        "/api/tests/suites",
        json={"name": "Runner Suite"},
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code == 403

    # Admin creates a suite so runner can trigger a run
    suite_resp = test_client.post(
        "/api/tests/suites",
        json={"name": "Runner Test Suite"},
        headers=_headers(ctx["admin_token"], ctx["org_id"]),
    )
    assert suite_resp.status_code == 200
    suite_id = suite_resp.json()["id"]

    # Runner can trigger a run (run_evals)
    resp = test_client.post(
        f"/api/tests/suites/{suite_id}/runs",
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    # Should not be 403 — run_evals grants run access
    assert resp.status_code != 403


# ═══════════════════════════════════════════════════════════════════════════
# 4. Role Stacking — multiple roles union their permissions
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
def test_role_stacking_unions_permissions(test_client, create_user, login_user, whoami, dynamic_sqlite_db, create_data_source):
    """User with Analyst + Instruction Author roles gets union of both permission sets."""
    ctx = _setup_org_with_member(test_client, create_user, login_user, whoami)

    if not _requires_enterprise(test_client, ctx["admin_token"], ctx["org_id"]):
        pytest.skip("Enterprise license required for custom roles")

    ds = create_data_source(
        name="stacking-test-ds",
        type="sqlite",
        config={"database": dynamic_sqlite_db},
        credentials={},
        user_token=ctx["admin_token"],
        org_id=ctx["org_id"],
    )

    # Role 1: Analyst (view-only)
    analyst = _create_custom_role(test_client, ctx["admin_token"], ctx["org_id"], "Stacking Analyst", [
        "view_reports", "view_evals", "export_query",
    ])

    # Role 2: Instruction Author (adds create_instructions)
    author = _create_custom_role(test_client, ctx["admin_token"], ctx["org_id"], "Stacking Author", [
        "view_instructions", "create_instructions",
    ])

    # Assign both roles to the member
    _assign_role(test_client, ctx["admin_token"], ctx["org_id"], analyst["id"], "user", ctx["member_id"])
    _assign_role(test_client, ctx["admin_token"], ctx["org_id"], author["id"], "user", ctx["member_id"])

    # Check resolved permissions — should be union
    perms = _get_whoami_perms(whoami, ctx["member_token"], ctx["org_id"])

    # From analyst role
    assert "view_reports" in perms["permissions"]
    assert "view_evals" in perms["permissions"]
    assert "export_query" in perms["permissions"]

    # From author role
    assert "view_instructions" in perms["permissions"]
    assert "create_instructions" in perms["permissions"]

    # org-level create_instructions → can create on any DS (two-tier OR)
    resp = test_client.post(
        "/api/instructions",
        json={
            "text": "Stacked role instruction",
            "status": "draft",
            "data_source_ids": [ds["id"]],
        },
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# 5. Group-based Inheritance
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
def test_group_role_inheritance(test_client, create_user, login_user, whoami):
    """User inherits permissions from a role assigned to their group."""
    ctx = _setup_org_with_member(test_client, create_user, login_user, whoami)

    if not _requires_enterprise(test_client, ctx["admin_token"], ctx["org_id"]):
        pytest.skip("Enterprise license required for groups")

    # Create a custom role
    viewer_role = _create_custom_role(test_client, ctx["admin_token"], ctx["org_id"], "Group Viewer", [
        "view_reports", "view_evals", "view_instructions", "view_entities",
    ])

    # Create a group
    group_resp = test_client.post(
        f"/api/organizations/{ctx['org_id']}/groups",
        json={"name": "Engineering"},
        headers=_headers(ctx["admin_token"], ctx["org_id"]),
    )
    if group_resp.status_code == 402:
        pytest.skip("Enterprise license required for groups")
    assert group_resp.status_code == 200
    group_id = group_resp.json()["id"]

    # Add member to group
    test_client.post(
        f"/api/organizations/{ctx['org_id']}/groups/{group_id}/members",
        json={"user_id": ctx["member_id"]},
        headers=_headers(ctx["admin_token"], ctx["org_id"]),
    )

    # Assign role to group (not directly to user)
    _assign_role(test_client, ctx["admin_token"], ctx["org_id"], viewer_role["id"], "group", group_id)

    # Member should inherit the group's role permissions
    perms = _get_whoami_perms(whoami, ctx["member_token"], ctx["org_id"])
    assert "view_reports" in perms["permissions"]
    assert "view_evals" in perms["permissions"]
    assert "view_instructions" in perms["permissions"]
    assert "view_entities" in perms["permissions"]

    # Still should NOT have admin perms
    assert "create_data_source" not in perms["permissions"]
    assert "manage_evals" not in perms["permissions"]


@pytest.mark.e2e
def test_group_resource_grant_inheritance(test_client, create_user, login_user, whoami, dynamic_sqlite_db, create_data_source):
    """User inherits resource grants from their group."""
    ctx = _setup_org_with_member(test_client, create_user, login_user, whoami)

    if not _requires_enterprise(test_client, ctx["admin_token"], ctx["org_id"]):
        pytest.skip("Enterprise license required for groups")

    ds = create_data_source(
        name="group-grant-ds",
        type="sqlite",
        config={"database": dynamic_sqlite_db},
        credentials={},
        user_token=ctx["admin_token"],
        org_id=ctx["org_id"],
    )

    # Create group
    group_resp = test_client.post(
        f"/api/organizations/{ctx['org_id']}/groups",
        json={"name": "Data Team"},
        headers=_headers(ctx["admin_token"], ctx["org_id"]),
    )
    if group_resp.status_code == 402:
        pytest.skip("Enterprise license required for groups")
    assert group_resp.status_code == 200
    group_id = group_resp.json()["id"]

    # Add member to group
    test_client.post(
        f"/api/organizations/{ctx['org_id']}/groups/{group_id}/members",
        json={"user_id": ctx["member_id"]},
        headers=_headers(ctx["admin_token"], ctx["org_id"]),
    )

    # Grant resource permissions to the GROUP
    _grant_resource(
        test_client, ctx["admin_token"], ctx["org_id"],
        "data_source", ds["id"], "group", group_id,
        ["query", "view_schema", "create_instructions"],
    )

    # Also give the member a role with view_instructions so they can hit the create endpoint
    viewer_role = _create_custom_role(test_client, ctx["admin_token"], ctx["org_id"], "Instruction Viewer", [
        "view_instructions",
    ])
    _assign_role(test_client, ctx["admin_token"], ctx["org_id"], viewer_role["id"], "user", ctx["member_id"])

    # Member should inherit group's resource grant
    perms = _get_whoami_perms(whoami, ctx["member_token"], ctx["org_id"])
    ds_key = f"data_source:{ds['id']}"
    assert ds_key in perms["resource_permissions"]
    ds_perms = set(perms["resource_permissions"][ds_key])
    assert "query" in ds_perms
    assert "create_instructions" in ds_perms


# ═══════════════════════════════════════════════════════════════════════════
# 6. Data Source Admin — full access on specific DS
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
def test_ds_admin_full_resource_access(test_client, create_user, login_user, whoami, dynamic_sqlite_db, create_data_source):
    """DS Admin has full resource permissions on a specific DS but no org-wide admin."""
    ctx = _setup_org_with_member(test_client, create_user, login_user, whoami)

    if not _requires_enterprise(test_client, ctx["admin_token"], ctx["org_id"]):
        pytest.skip("Enterprise license required for custom roles")

    ds = create_data_source(
        name="ds-admin-test",
        type="sqlite",
        config={"database": dynamic_sqlite_db},
        credentials={},
        user_token=ctx["admin_token"],
        org_id=ctx["org_id"],
    )

    # Minimal org-level permissions
    ds_admin_role = _create_custom_role(test_client, ctx["admin_token"], ctx["org_id"], "DS Admin", [
        "view_reports", "view_data_source", "view_instructions",
    ])
    _assign_role(test_client, ctx["admin_token"], ctx["org_id"], ds_admin_role["id"], "user", ctx["member_id"])

    # Full resource grant on the DS
    _grant_resource(
        test_client, ctx["admin_token"], ctx["org_id"],
        "data_source", ds["id"], "user", ctx["member_id"],
        ["query", "view_schema", "manage", "manage_members",
         "create_instructions", "view_instructions",
         "create_entities", "view_entities",
         "run_evals", "view_evals"],
    )

    perms = _get_whoami_perms(whoami, ctx["member_token"], ctx["org_id"])

    # Org-level: only view
    assert "view_reports" in perms["permissions"]
    assert "create_data_source" not in perms["permissions"]
    assert "full_admin_access" not in perms["permissions"]

    # Resource-level: full access on the DS
    ds_key = f"data_source:{ds['id']}"
    assert ds_key in perms["resource_permissions"]
    ds_perms = set(perms["resource_permissions"][ds_key])
    assert ds_perms == {
        "query", "view_schema", "manage", "manage_members",
        "create_instructions", "view_instructions",
        "create_entities", "view_entities",
        "run_evals", "view_evals",
    }

    # DS Admin can create instructions on their DS (resource grant)
    resp = test_client.post(
        "/api/instructions",
        json={
            "text": "DS admin instruction",
            "status": "draft",
            "data_source_ids": [ds["id"]],
        },
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code == 200

    # But denied on a different DS (no grant, no org-level create_instructions)
    resp = test_client.post(
        "/api/instructions",
        json={
            "text": "Denied instruction",
            "status": "draft",
            "data_source_ids": [str(uuid.uuid4())],
        },
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# 7. Negative Cases — permission boundaries
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
def test_no_role_no_access(test_client, create_user, login_user, whoami):
    """User with no custom role falls back to legacy member permissions."""
    ctx = _setup_org_with_member(test_client, create_user, login_user, whoami)

    # Member has default member role (no custom role assigned)
    perms = _get_whoami_perms(whoami, ctx["member_token"], ctx["org_id"])

    # Should have basic member perms from fallback
    assert "view_reports" in perms["permissions"]

    # Should NOT have admin-only perms
    assert "create_data_source" not in perms["permissions"]
    assert "manage_evals" not in perms["permissions"]
    assert "manage_roles" not in perms["permissions"]


@pytest.mark.e2e
def test_resource_grant_without_role_still_resolves(test_client, create_user, login_user, whoami, dynamic_sqlite_db, create_data_source):
    """Resource grant on a DS should appear in resolution even without a custom role."""
    ctx = _setup_org_with_member(test_client, create_user, login_user, whoami)

    ds = create_data_source(
        name="grant-only-ds",
        type="sqlite",
        config={"database": dynamic_sqlite_db},
        credentials={},
        user_token=ctx["admin_token"],
        org_id=ctx["org_id"],
    )

    # Grant resource permission directly (no custom role)
    _grant_resource(
        test_client, ctx["admin_token"], ctx["org_id"],
        "data_source", ds["id"], "user", ctx["member_id"],
        ["query", "view_schema"],
    )

    perms = _get_whoami_perms(whoami, ctx["member_token"], ctx["org_id"])
    ds_key = f"data_source:{ds['id']}"
    assert ds_key in perms["resource_permissions"]
    assert "query" in perms["resource_permissions"][ds_key]


@pytest.mark.e2e
def test_mixed_grants_on_multiple_ds(test_client, create_user, login_user, whoami, dynamic_sqlite_db, create_data_source):
    """Different permission sets on different data sources resolve independently."""
    ctx = _setup_org_with_member(test_client, create_user, login_user, whoami)

    ds1 = create_data_source(
        name="multi-ds-1",
        type="sqlite",
        config={"database": dynamic_sqlite_db},
        credentials={},
        user_token=ctx["admin_token"],
        org_id=ctx["org_id"],
    )
    ds2_id = str(uuid.uuid4())  # Fake second DS

    # Grant different permissions on each
    _grant_resource(
        test_client, ctx["admin_token"], ctx["org_id"],
        "data_source", ds1["id"], "user", ctx["member_id"],
        ["query", "view_schema", "create_instructions"],
    )
    _grant_resource(
        test_client, ctx["admin_token"], ctx["org_id"],
        "data_source", ds2_id, "user", ctx["member_id"],
        ["query"],
    )

    perms = _get_whoami_perms(whoami, ctx["member_token"], ctx["org_id"])

    # DS1 has full grant
    ds1_key = f"data_source:{ds1['id']}"
    assert "create_instructions" in perms["resource_permissions"].get(ds1_key, [])

    # DS2 has only query
    ds2_key = f"data_source:{ds2_id}"
    ds2_perms = set(perms["resource_permissions"].get(ds2_key, []))
    assert "query" in ds2_perms
    assert "create_instructions" not in ds2_perms


# ═══════════════════════════════════════════════════════════════════════════
# 8. Mixed DS list — partial access denied (all-or-nothing)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
def test_instruction_mixed_ds_list_denied(test_client, create_user, login_user, whoami, dynamic_sqlite_db, create_data_source):
    """Creating an instruction with [granted_ds, denied_ds] should be denied entirely."""
    ctx = _setup_org_with_member(test_client, create_user, login_user, whoami)

    if not _requires_enterprise(test_client, ctx["admin_token"], ctx["org_id"]):
        pytest.skip("Enterprise license required for custom roles")

    ds_granted = create_data_source(
        name="mixed-instr-granted",
        type="sqlite",
        config={"database": dynamic_sqlite_db},
        credentials={},
        user_token=ctx["admin_token"],
        org_id=ctx["org_id"],
    )
    ds_denied_id = str(uuid.uuid4())

    # Give user view perms org-wide
    role = _create_custom_role(test_client, ctx["admin_token"], ctx["org_id"], "Mixed Instr Author", [
        "view_instructions",
    ])
    _assign_role(test_client, ctx["admin_token"], ctx["org_id"], role["id"], "user", ctx["member_id"])

    # Grant create_instructions on only one DS
    _grant_resource(
        test_client, ctx["admin_token"], ctx["org_id"],
        "data_source", ds_granted["id"], "user", ctx["member_id"],
        ["query", "view_schema", "create_instructions"],
    )

    # Single granted DS — should succeed
    resp = test_client.post(
        "/api/instructions",
        json={
            "text": "Single DS instruction",
            "status": "draft",
            "data_source_ids": [ds_granted["id"]],
        },
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code == 200, f"Single granted DS should succeed: {resp.text}"

    # Mixed list [granted, denied] — should be denied entirely
    resp = test_client.post(
        "/api/instructions",
        json={
            "text": "Mixed DS instruction",
            "status": "draft",
            "data_source_ids": [ds_granted["id"], ds_denied_id],
        },
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code == 403, "Mixed DS list should be denied when any DS lacks permission"


@pytest.mark.e2e
def test_entity_mixed_ds_list_denied(test_client, create_user, login_user, whoami, dynamic_sqlite_db, create_data_source):
    """Creating an entity with [granted_ds, denied_ds] should be denied entirely."""
    ctx = _setup_org_with_member(test_client, create_user, login_user, whoami)

    if not _requires_enterprise(test_client, ctx["admin_token"], ctx["org_id"]):
        pytest.skip("Enterprise license required for custom roles")

    ds_granted = create_data_source(
        name="mixed-entity-granted",
        type="sqlite",
        config={"database": dynamic_sqlite_db},
        credentials={},
        user_token=ctx["admin_token"],
        org_id=ctx["org_id"],
    )
    ds_denied_id = str(uuid.uuid4())

    role = _create_custom_role(test_client, ctx["admin_token"], ctx["org_id"], "Mixed Entity Author", [
        "view_entities",
    ])
    _assign_role(test_client, ctx["admin_token"], ctx["org_id"], role["id"], "user", ctx["member_id"])

    # Grant create_entities on only one DS
    _grant_resource(
        test_client, ctx["admin_token"], ctx["org_id"],
        "data_source", ds_granted["id"], "user", ctx["member_id"],
        ["query", "view_schema", "create_entities"],
    )

    # Mixed list [granted, denied] — should be denied
    resp = test_client.post(
        "/api/entities",
        json={
            "type": "model",
            "title": "Mixed DS Entity",
            "slug": "mixed-ds-entity",
            "code": "SELECT 1",
            "data_source_ids": [ds_granted["id"], ds_denied_id],
        },
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code == 403, "Mixed DS list should be denied when any DS lacks permission"

    # Single granted DS — should succeed (or at least not 403)
    resp = test_client.post(
        "/api/entities",
        json={
            "type": "model",
            "title": "Single DS Entity",
            "slug": "single-ds-entity",
            "code": "SELECT 1",
            "data_source_ids": [ds_granted["id"]],
        },
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code != 403, f"Single granted DS should not be 403: {resp.text}"


@pytest.mark.e2e
def test_eval_case_mixed_ds_list_denied(test_client, create_user, login_user, whoami, dynamic_sqlite_db, create_data_source):
    """Creating an eval case with [granted_ds, denied_ds] should be denied entirely."""
    ctx = _setup_org_with_member(test_client, create_user, login_user, whoami)

    if not _requires_enterprise(test_client, ctx["admin_token"], ctx["org_id"]):
        pytest.skip("Enterprise license required for custom roles")

    ds_granted = create_data_source(
        name="mixed-eval-granted",
        type="sqlite",
        config={"database": dynamic_sqlite_db},
        credentials={},
        user_token=ctx["admin_token"],
        org_id=ctx["org_id"],
    )
    ds_denied_id = str(uuid.uuid4())

    role = _create_custom_role(test_client, ctx["admin_token"], ctx["org_id"], "Mixed Eval Author", [
        "view_evals",
    ])
    _assign_role(test_client, ctx["admin_token"], ctx["org_id"], role["id"], "user", ctx["member_id"])

    # Grant create_evals on only one DS
    _grant_resource(
        test_client, ctx["admin_token"], ctx["org_id"],
        "data_source", ds_granted["id"], "user", ctx["member_id"],
        ["query", "view_schema", "run_evals"],
    )

    # Admin creates a suite (member can't — needs manage_evals)
    suite_resp = test_client.post(
        "/api/tests/suites",
        json={"name": "Mixed Eval Suite"},
        headers=_headers(ctx["admin_token"], ctx["org_id"]),
    )
    assert suite_resp.status_code == 200
    suite_id = suite_resp.json()["id"]

    # Member tries to create case with mixed DS list — should be denied
    resp = test_client.post(
        f"/api/tests/suites/{suite_id}/cases",
        json={
            "name": "Mixed DS Case",
            "prompt_json": {"content": "Test prompt"},
            "expectations_json": {"spec_version": 1, "rules": [], "order_mode": "flexible"},
            "data_source_ids_json": [ds_granted["id"], ds_denied_id],
        },
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code == 403, "Mixed DS list should be denied when any DS lacks permission"

    # Single granted DS — should succeed (resource_scoped lets through decorator, grant allows DS check)
    resp = test_client.post(
        f"/api/tests/suites/{suite_id}/cases",
        json={
            "name": "Single DS Case",
            "prompt_json": {"content": "Test prompt"},
            "expectations_json": {"spec_version": 1, "rules": [], "order_mode": "flexible"},
            "data_source_ids_json": [ds_granted["id"]],
        },
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    # resource_scoped=True on manage_evals lets through, but create_evals != run_evals
    # The grant has run_evals, but the route checks create_evals — so this should be denied
    assert resp.status_code == 403, "Grant has run_evals but route checks create_evals"
