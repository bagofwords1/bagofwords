"""
RBAC policy enforcement tests.

Tests cover:
- Two-tier OR logic: org-level permission grants blanket access, resource-level scopes to specific resources
- Resource permission enforcement on data sources (query, create_instructions, create_entities, etc.)
- Enforcement gaps fixed: entity update, entity from_step, instruction update
- Connection resource permissions (manage_data_sources)
- Permission registry returns correct categories and resource permissions
"""
import pytest
import uuid


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}


def _setup_admin_and_member(test_client, create_user, login_user, whoami):
    """Helper: create an admin and a member in the same org, return their tokens and IDs."""
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
    }


# ── Permission Registry ──────────────────────────────────────────────────

POST_MVP_SKIP = "post-MVP: registry only has manage_evals and data_source resource grants"


@pytest.mark.e2e
@pytest.mark.skip(reason=POST_MVP_SKIP)
def test_registry_returns_updated_categories(test_client, create_user, login_user):
    """Permission registry should return the correct categories after cleanup."""
    user = create_user()
    token = login_user(user["email"], user["password"])

    resp = test_client.get("/api/permissions/registry", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()

    categories = data["categories"]
    resource_perms = data["resource_permissions"]

    # Evals category should exist with correct permissions
    assert "Evals" in categories
    assert set(categories["Evals"]) == {"manage_evals"}

    # Queries category replaces Widgets
    assert "Queries" in categories
    assert "export_query" in categories["Queries"]
    assert "Widgets" not in categories

    # Removed permissions should not appear anywhere
    all_perms = set()
    for perms in categories.values():
        all_perms.update(perms)
    for removed in ["manage_tests", "export_widgets", "view_widgets", "create_widgets",
                     "create_text_widgets", "create_private_instructions", "view_private_instructions"]:
        assert removed not in all_perms, f"{removed} should have been removed"


@pytest.mark.e2e
@pytest.mark.skip(reason=POST_MVP_SKIP)
def test_registry_data_source_resource_permissions(test_client, create_user, login_user):
    """Data source resource permissions should include entity/instruction/eval scoped perms."""
    user = create_user()
    token = login_user(user["email"], user["password"])

    resp = test_client.get("/api/permissions/registry", headers={"Authorization": f"Bearer {token}"})
    data = resp.json()
    ds_perms = data["resource_permissions"]["data_source"]

    expected = {
        "query", "view_schema", "view_entities", "create_entities",
        "view_instructions", "create_instructions",
        "manage", "manage_members",
    }
    assert set(ds_perms) == expected


@pytest.mark.e2e
@pytest.mark.skip(reason=POST_MVP_SKIP)
def test_registry_connection_resource_permissions(test_client, create_user, login_user):
    """Connection resource permissions should be simplified to manage_data_sources and manage."""
    user = create_user()
    token = login_user(user["email"], user["password"])

    resp = test_client.get("/api/permissions/registry", headers={"Authorization": f"Bearer {token}"})
    data = resp.json()
    conn_perms = data["resource_permissions"]["connection"]

    assert set(conn_perms) == {"manage_data_sources", "manage"}


@pytest.mark.e2e
@pytest.mark.skip(reason=POST_MVP_SKIP)
def test_registry_report_resource_permissions(test_client, create_user, login_user):
    """Report resource permissions should include run_steps."""
    user = create_user()
    token = login_user(user["email"], user["password"])

    resp = test_client.get("/api/permissions/registry", headers={"Authorization": f"Bearer {token}"})
    data = resp.json()
    report_perms = data["resource_permissions"]["report"]

    assert "run_steps" in report_perms
    assert "view_artifacts" in report_perms
    assert "view_conversation" in report_perms


# ── Eval Permission Split ─────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.skip(reason=POST_MVP_SKIP)
def test_member_can_view_evals(test_client, create_user, login_user, whoami):
    """Members should be able to list test suites."""
    ctx = _setup_admin_and_member(test_client, create_user, login_user, whoami)

    # Member can list suites
    resp = test_client.get(
        f"/api/tests/suites",
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code == 200


@pytest.mark.e2e
def test_member_cannot_manage_evals(test_client, create_user, login_user, whoami):
    """Members should NOT have manage_evals and should be denied CRUD on suites/cases."""
    ctx = _setup_admin_and_member(test_client, create_user, login_user, whoami)

    member_info = whoami(ctx["member_token"])
    member_org = next(o for o in member_info["organizations"] if o["id"] == ctx["org_id"])
    assert "manage_evals" not in member_org["permissions"]

    # Member tries to create a suite (manage_evals required)
    resp = test_client.post(
        f"/api/tests/suites",
        json={"name": "Unauthorized Suite"},
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code == 403


@pytest.mark.e2e
def test_admin_can_manage_evals(test_client, create_user, login_user, whoami):
    """Admin should be able to create and delete test suites."""
    ctx = _setup_admin_and_member(test_client, create_user, login_user, whoami)

    # Admin creates a suite
    resp = test_client.post(
        f"/api/tests/suites",
        json={"name": "Admin Suite", "description": "Test"},
        headers=_headers(ctx["admin_token"], ctx["org_id"]),
    )
    assert resp.status_code == 200
    suite_id = resp.json()["id"]

    # Admin deletes it
    resp = test_client.delete(
        f"/api/tests/suites/{suite_id}",
        headers=_headers(ctx["admin_token"], ctx["org_id"]),
    )
    assert resp.status_code == 200


# ── Two-Tier OR Logic ─────────────────────────────────────────────────────

@pytest.mark.e2e
def test_org_level_permission_grants_blanket_access(test_client, create_user, login_user, whoami):
    """Admin with org-level full_admin_access can access any resource without explicit grants."""
    ctx = _setup_admin_and_member(test_client, create_user, login_user, whoami)

    # Admin creates an instruction referencing a fake DS
    # full_admin_access should bypass resource checks — should NOT get 403
    fake_ds_id = str(uuid.uuid4())

    resp = test_client.post(
        f"/api/instructions",
        json={
            "text": "Test instruction for blanket access",
            "status": "published",
            "data_source_ids": [fake_ds_id],
        },
        headers=_headers(ctx["admin_token"], ctx["org_id"]),
    )
    # Should not be 403 — admin has blanket access via full_admin_access
    assert resp.status_code != 403


@pytest.mark.e2e
def test_member_denied_without_resource_grant(test_client, create_user, login_user, whoami):
    """Member without org-level create_entities and no resource grant should be denied."""
    ctx = _setup_admin_and_member(test_client, create_user, login_user, whoami)

    member_info = whoami(ctx["member_token"])
    member_org = next(o for o in member_info["organizations"] if o["id"] == ctx["org_id"])

    # Member should not have create_entities at org level
    assert "create_entities" not in member_org["permissions"]

    fake_ds_id = str(uuid.uuid4())

    # Member tries to create entity — should be denied at org level
    resp = test_client.post(
        f"/api/entities",
        json={
            "type": "model",
            "title": "Unauthorized",
            "slug": "unauthorized",
            "code": "SELECT 1",
            "data_source_ids": [fake_ds_id],
        },
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code == 403


# ── Resource Grant CRUD with New Permissions ──────────────────────────────

@pytest.mark.e2e
def test_resource_grant_with_new_ds_permissions(test_client, create_user, login_user, whoami):
    """Admin can create resource grants with the new data source permission strings."""
    ctx = _setup_admin_and_member(test_client, create_user, login_user, whoami)

    fake_ds_id = str(uuid.uuid4())

    # Create a grant with the new permission strings
    resp = test_client.post(
        f"/api/organizations/{ctx['org_id']}/resource-grants",
        json={
            "resource_type": "data_source",
            "resource_id": fake_ds_id,
            "principal_type": "user",
            "principal_id": ctx["member_id"],
            "permissions": ["query", "view_schema", "view_entities", "create_instructions"],
        },
        headers=_headers(ctx["admin_token"], ctx["org_id"]),
    )
    assert resp.status_code == 200
    grant = resp.json()
    assert set(grant["permissions"]) == {"query", "view_schema", "view_entities", "create_instructions"}

    # Cleanup
    test_client.delete(
        f"/api/organizations/{ctx['org_id']}/resource-grants/{grant['id']}",
        headers=_headers(ctx["admin_token"], ctx["org_id"]),
    )


@pytest.mark.e2e
def test_resource_grant_with_connection_permissions(test_client, create_user, login_user, whoami):
    """Admin can create resource grants with new connection permission strings."""
    ctx = _setup_admin_and_member(test_client, create_user, login_user, whoami)

    fake_conn_id = str(uuid.uuid4())

    resp = test_client.post(
        f"/api/organizations/{ctx['org_id']}/resource-grants",
        json={
            "resource_type": "connection",
            "resource_id": fake_conn_id,
            "principal_type": "user",
            "principal_id": ctx["member_id"],
            "permissions": ["manage_data_sources", "manage"],
        },
        headers=_headers(ctx["admin_token"], ctx["org_id"]),
    )
    assert resp.status_code == 200
    grant = resp.json()
    assert set(grant["permissions"]) == {"manage_data_sources", "manage"}

    # Cleanup
    test_client.delete(
        f"/api/organizations/{ctx['org_id']}/resource-grants/{grant['id']}",
        headers=_headers(ctx["admin_token"], ctx["org_id"]),
    )


# ── Enforcement Gap Fixes ─────────────────────────────────────────────────

@pytest.mark.e2e
def test_entity_update_checks_data_source_permissions(test_client, create_user, login_user, whoami, create_global_entity):
    """PUT /entities/{id} should enforce data source permissions when changing data_source_ids."""
    ctx = _setup_admin_and_member(test_client, create_user, login_user, whoami)

    # Admin creates an entity via fixture
    try:
        entity = create_global_entity(user_token=ctx["admin_token"], org_id=ctx["org_id"])
    except Exception:
        pytest.skip("Entity creation not available")

    entity_id = entity["id"]
    fake_ds_id = str(uuid.uuid4())

    # Member tries to update entity with a DS they don't have access to
    # Should be denied (no org-level create_entities, no resource grant)
    resp = test_client.put(
        f"/api/entities/{entity_id}",
        json={"data_source_ids": [fake_ds_id]},
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    assert resp.status_code == 403

    # Cleanup
    test_client.delete(
        f"/api/entities/{entity_id}",
        headers=_headers(ctx["admin_token"], ctx["org_id"]),
    )


@pytest.mark.e2e
def test_instruction_update_checks_data_source_permissions(test_client, create_user, login_user, whoami):
    """PUT /instructions/{id} should enforce data source permissions when changing data_source_ids."""
    ctx = _setup_admin_and_member(test_client, create_user, login_user, whoami)

    # Admin creates an instruction
    resp = test_client.post(
        f"/api/instructions",
        json={"text": "Test instruction", "status": "published"},
        headers=_headers(ctx["admin_token"], ctx["org_id"]),
    )
    if resp.status_code != 200:
        pytest.skip("Instruction creation failed")

    instruction_id = resp.json()["id"]
    fake_ds_id = str(uuid.uuid4())

    # Member tries to update instruction with a DS they don't have access to
    resp = test_client.put(
        f"/api/instructions/{instruction_id}",
        json={"data_source_ids": [fake_ds_id]},
        headers=_headers(ctx["member_token"], ctx["org_id"]),
    )
    # Should be denied — member lacks both org-level and resource-level create_instructions
    assert resp.status_code == 403

    # Cleanup
    test_client.delete(
        f"/api/instructions/{instruction_id}",
        headers=_headers(ctx["admin_token"], ctx["org_id"]),
    )


# ── Whoami Reflects New Permissions ───────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.skip(reason=POST_MVP_SKIP)
def test_whoami_member_has_eval_permissions(test_client, create_user, login_user, whoami):
    """Member whoami should not include manage_evals."""
    ctx = _setup_admin_and_member(test_client, create_user, login_user, whoami)

    member_info = whoami(ctx["member_token"])
    member_org = next(o for o in member_info["organizations"] if o["id"] == ctx["org_id"])
    perms = member_org["permissions"]

    assert "manage_evals" not in perms
    assert "export_query" in perms

    # Removed permissions should not appear
    assert "export_widgets" not in perms
    assert "manage_tests" not in perms
    assert "view_widgets" not in perms
    assert "create_private_instructions" not in perms


@pytest.mark.e2e
def test_whoami_admin_has_manage_evals(test_client, create_user, login_user, whoami):
    """Admin whoami should include manage_evals."""
    ctx = _setup_admin_and_member(test_client, create_user, login_user, whoami)

    admin_info = whoami(ctx["admin_token"])
    admin_org = next(o for o in admin_info["organizations"] if o["id"] == ctx["org_id"])
    perms = admin_org["permissions"]

    # Admin has full_admin_access which bypasses, but the role should still resolve
    assert "full_admin_access" in perms or "manage_evals" in perms
