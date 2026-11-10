"""Editable default `member` role (enterprise).

Contract under test (see rbac_service / permission_resolver):

- Editing the built-in `member` role materializes a per-org, editable override
  and re-points this org's member assignments to it, so an admin's permission
  edits reach existing members immediately.
- Members invited AFTER the customization inherit the override too (provisioning
  resolves the override, not the shipped default).
- Permissions an admin ADDS to member reach every member; withholdable
  permissions an admin REMOVES are taken away.
- The override appears exactly once in the role list (not duplicated with the
  global default), is flagged editable-but-not-deletable, and cannot be deleted.
- `admin` and other system roles stay locked; a custom role may not usurp a
  reserved built-in name.

These run under the e2e session's forced enterprise license (custom_roles).
"""
import uuid

import pytest


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}


def _admin_org(create_user, login_user, whoami):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return token, org_id


def _invite_and_register(test_client, create_user, login_user, whoami, admin_token, org_id):
    """Invite a member, register the account, return (member_token, perms)."""
    email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    resp = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email, "role": "member"},
        headers=_headers(admin_token, org_id),
    )
    assert resp.status_code in (200, 201), resp.json()
    create_user(email=email, password="test123")
    token = login_user(email, "test123")
    perms = _org_perms(whoami(token), org_id)
    return token, perms


def _org_perms(info, org_id):
    org = next(o for o in info["organizations"] if o["id"] == org_id)
    return org["permissions"]


def _member_role(test_client, admin_token, org_id):
    roles = test_client.get(
        f"/api/organizations/{org_id}/roles", headers=_headers(admin_token, org_id)
    ).json()
    return next(r for r in roles if r["name"] == "member")


def _put_role(test_client, admin_token, org_id, role_id, **body):
    return test_client.put(
        f"/api/organizations/{org_id}/roles/{role_id}",
        json=body,
        headers=_headers(admin_token, org_id),
    )


# ── Granting ───────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_editing_member_grants_reach_existing_members(
    test_client, create_user, login_user, whoami
):
    admin_token, org_id = _admin_org(create_user, login_user, whoami)
    member_token, perms = _invite_and_register(
        test_client, create_user, login_user, whoami, admin_token, org_id
    )
    # Baseline: a member does not manage instructions.
    assert "manage_instructions" not in perms

    member_role = _member_role(test_client, admin_token, org_id)
    assert member_role["is_system"] is True  # still the global default here

    new_perms = sorted(set(member_role["permissions"]) | {"manage_instructions"})
    resp = _put_role(test_client, admin_token, org_id, member_role["id"], permissions=new_perms)
    assert resp.status_code == 200, resp.json()

    # The existing member now holds the added permission — no re-login needed
    # beyond a fresh whoami (permissions resolve per request).
    after = _org_perms(whoami(member_token), org_id)
    assert "manage_instructions" in after


@pytest.mark.e2e
def test_new_member_after_customization_inherits_override(
    test_client, create_user, login_user, whoami
):
    admin_token, org_id = _admin_org(create_user, login_user, whoami)
    member_role = _member_role(test_client, admin_token, org_id)
    new_perms = sorted(set(member_role["permissions"]) | {"manage_instructions"})
    assert _put_role(
        test_client, admin_token, org_id, member_role["id"], permissions=new_perms
    ).status_code == 200

    # A member provisioned AFTER the edit must inherit the customized set.
    _token, perms = _invite_and_register(
        test_client, create_user, login_user, whoami, admin_token, org_id
    )
    assert "manage_instructions" in perms


@pytest.mark.e2e
def test_withholding_default_on_permission_from_members(
    test_client, create_user, login_user, whoami
):
    admin_token, org_id = _admin_org(create_user, login_user, whoami)
    member_token, perms = _invite_and_register(
        test_client, create_user, login_user, whoami, admin_token, org_id
    )
    # view_code is granted to members by default (withholdable, not baseline).
    assert "view_code" in perms

    member_role = _member_role(test_client, admin_token, org_id)
    # Drop both code permissions: run_custom_code implies view_code at the
    # resolver (ORG_PERM_IMPLIES_ORG), so withholding view_code alone would be
    # re-derived. Removing both is what actually takes code visibility away.
    kept = [p for p in member_role["permissions"] if p not in ("view_code", "run_custom_code")]
    assert _put_role(
        test_client, admin_token, org_id, member_role["id"], permissions=kept
    ).status_code == 200

    after = _org_perms(whoami(member_token), org_id)
    assert "view_code" not in after
    # A baseline permission is untouched — it is granted by the resolver, not
    # the role, so members keep ordinary product usage.
    assert "view_reports" in after


# ── Shape / flags ────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_member_override_listed_once_with_flags(
    test_client, create_user, login_user, whoami
):
    admin_token, org_id = _admin_org(create_user, login_user, whoami)
    member_role = _member_role(test_client, admin_token, org_id)
    new_perms = sorted(set(member_role["permissions"]) | {"manage_instructions"})
    assert _put_role(
        test_client, admin_token, org_id, member_role["id"], permissions=new_perms
    ).status_code == 200

    roles = test_client.get(
        f"/api/organizations/{org_id}/roles", headers=_headers(admin_token, org_id)
    ).json()
    members = [r for r in roles if r["name"] == "member"]
    assert len(members) == 1, "member must not be duplicated by the override"
    m = members[0]
    assert m["is_system"] is False
    assert m["overrides_system_role"] == "member"
    assert m["is_editable"] is True
    assert m["is_deletable"] is False


@pytest.mark.e2e
def test_member_builtin_is_editable_before_customization(
    test_client, create_user, login_user, whoami
):
    admin_token, org_id = _admin_org(create_user, login_user, whoami)
    member_role = _member_role(test_client, admin_token, org_id)
    # The shipped global default advertises itself as editable (editing it
    # materializes the override) but never deletable.
    assert member_role["is_system"] is True
    assert member_role["is_editable"] is True
    assert member_role["is_deletable"] is False


# ── Guards ───────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_member_override_cannot_be_deleted(
    test_client, create_user, login_user, whoami
):
    admin_token, org_id = _admin_org(create_user, login_user, whoami)
    member_role = _member_role(test_client, admin_token, org_id)
    assert _put_role(
        test_client, admin_token, org_id, member_role["id"],
        permissions=member_role["permissions"],
    ).status_code == 200

    override = _member_role(test_client, admin_token, org_id)
    resp = test_client.delete(
        f"/api/organizations/{org_id}/roles/{override['id']}",
        headers=_headers(admin_token, org_id),
    )
    assert resp.status_code == 403


@pytest.mark.e2e
def test_admin_role_stays_locked(test_client, create_user, login_user, whoami):
    admin_token, org_id = _admin_org(create_user, login_user, whoami)
    roles = test_client.get(
        f"/api/organizations/{org_id}/roles", headers=_headers(admin_token, org_id)
    ).json()
    admin_role = next(r for r in roles if r["name"] == "admin")
    assert admin_role["is_editable"] is False
    resp = _put_role(
        test_client, admin_token, org_id, admin_role["id"],
        permissions=["view_reports"],
    )
    assert resp.status_code == 403


@pytest.mark.e2e
def test_cannot_create_role_with_reserved_name(
    test_client, create_user, login_user, whoami
):
    admin_token, org_id = _admin_org(create_user, login_user, whoami)
    resp = test_client.post(
        f"/api/organizations/{org_id}/roles",
        json={"name": "member", "permissions": ["view_reports"]},
        headers=_headers(admin_token, org_id),
    )
    assert resp.status_code == 422
