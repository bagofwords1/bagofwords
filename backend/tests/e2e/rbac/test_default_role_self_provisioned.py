"""The org-wide default role reaches members who join on their own.

The "Default role" picker on the Members page (``default_member_role``) must
apply to everyone who becomes a member without an admin picking a role, not
only to the "Add member" modal: SCIM provisioning and domain sign-up (and the
OIDC/LDAP/SAML/Entra paths that share the same helper) land new members with
that role, as a real role assignment.

Guard rails:
* nothing configured -> plain ``member``, as before;
* a role that carries ``full_admin_access`` is never handed out automatically;
* the domain sign-up policy's own role still wins when it names one other
  than ``member``.
"""
import json
import uuid

import pytest

from app.settings.config import settings as bow_settings


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _custom_role(create_role, admin, prefix="default-access"):
    name = f"{prefix}-{uuid.uuid4().hex[:6]}"
    resp = create_role(
        name=name, permissions=["view_reports"],
        user_token=admin["token"], org_id=admin["org_id"],
    )
    assert resp.status_code in (200, 201), resp.text
    return name


def _set_default_role(test_client, admin, role):
    resp = test_client.put(
        "/api/organization/default-member-role", json={"role": role},
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.text


def _member_roles(test_client, admin, email):
    """Role names the members list shows for ``email`` (registered or pending)."""
    resp = test_client.get(
        f"/api/organizations/{admin['org_id']}/members",
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    for m in resp.json():
        user_email = ((m.get("user") or {}).get("email") or m.get("email") or "").lower()
        if user_email == email.lower():
            return {r["name"] for r in (m.get("roles") or [])}
    raise AssertionError(f"{email} is not a member")


@pytest.fixture
def scim_admin(test_client, bootstrap_admin, enterprise_license):
    """An org admin plus a function that provisions a user through SCIM."""
    admin = bootstrap_admin()
    resp = test_client.post(
        "/api/enterprise/scim/tokens", json={"name": "IdP"},
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert resp.status_code == 201, resp.text
    scim_token = resp.json()["token"]

    def provision(email):
        created = test_client.post(
            "/scim/v2/Users",
            content=json.dumps({
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": email, "displayName": "Provisioned", "active": True,
            }),
            headers={"Authorization": f"Bearer {scim_token}", "Content-Type": "application/scim+json"},
        )
        assert created.status_code == 201, created.text

    admin["provision"] = provision
    return admin


def _email(prefix="scim"):
    return f"{prefix}_{uuid.uuid4().hex[:10]}@test.com"


def test_scim_user_gets_the_org_default_custom_role(test_client, scim_admin, create_role):
    role = _custom_role(create_role, scim_admin)
    _set_default_role(test_client, scim_admin, role)

    emails = [_email(), _email()]
    for email in emails:
        scim_admin["provision"](email)

    for email in emails:
        roles = _member_roles(test_client, scim_admin, email)
        assert role in roles, roles
        assert "member" not in roles, roles


def test_scim_user_gets_member_when_no_default_is_set(test_client, scim_admin):
    email = _email()
    scim_admin["provision"](email)

    assert "member" in _member_roles(test_client, scim_admin, email)


def test_scim_never_auto_grants_a_full_admin_default(test_client, scim_admin):
    _set_default_role(test_client, scim_admin, "admin")

    email = _email()
    scim_admin["provision"](email)

    roles = _member_roles(test_client, scim_admin, email)
    assert "admin" not in roles, roles
    assert "member" in roles, roles


def test_default_role_changes_apply_to_later_provisioning(test_client, scim_admin, create_role):
    first, second = _custom_role(create_role, scim_admin, "first"), _custom_role(create_role, scim_admin, "second")

    _set_default_role(test_client, scim_admin, first)
    early = _email()
    scim_admin["provision"](early)
    _set_default_role(test_client, scim_admin, second)
    late = _email()
    scim_admin["provision"](late)

    assert first in _member_roles(test_client, scim_admin, early)
    late_roles = _member_roles(test_client, scim_admin, late)
    assert second in late_roles and first not in late_roles, late_roles


# ── Domain sign-up ──────────────────────────────────────────────────────


def _register(test_client, email, password="Test1234!"):
    resp = test_client.post(
        "/api/auth/register",
        json={"name": "walk-in", "email": email, "password": password},
    )
    assert resp.status_code == 201, resp.text


@pytest.fixture
def domain_admin(test_client, bootstrap_admin, enterprise_license, monkeypatch):
    """A single-organization deployment whose admin admits one email domain."""
    monkeypatch.setattr(bow_settings.bow_config.features, "allow_multiple_organizations", False)
    # Domain sign-up is a licensed feature the suite's stub license does not list.
    monkeypatch.setattr(enterprise_license, "features", [*enterprise_license.features, "domain_signup"])
    admin = bootstrap_admin()
    admin["domain"] = f"{uuid.uuid4().hex[:10]}.example"

    def set_policy(role="member"):
        resp = test_client.put(
            "/api/organization/signup-policy",
            json={"enabled": True, "allowed_domains": [admin["domain"]], "auto_invite_role": role},
            headers=_hdr(admin["token"], admin["org_id"]),
        )
        assert resp.status_code == 200, resp.text

    admin["set_policy"] = set_policy
    return admin


def test_domain_signup_uses_the_org_default_role(test_client, domain_admin, create_role):
    role = _custom_role(create_role, domain_admin)
    _set_default_role(test_client, domain_admin, role)
    domain_admin["set_policy"]()  # policy left on its own default, "member"

    email = f"walkin_{uuid.uuid4().hex[:8]}@{domain_admin['domain']}"
    _register(test_client, email)

    roles = _member_roles(test_client, domain_admin, email)
    assert role in roles, roles
    assert "member" not in roles, roles


def test_domain_signup_policy_role_wins_over_the_default(test_client, domain_admin, create_role):
    default = _custom_role(create_role, domain_admin, "default")
    policy_role = _custom_role(create_role, domain_admin, "policy")
    _set_default_role(test_client, domain_admin, default)
    domain_admin["set_policy"](policy_role)

    email = f"walkin_{uuid.uuid4().hex[:8]}@{domain_admin['domain']}"
    _register(test_client, email)

    roles = _member_roles(test_client, domain_admin, email)
    assert policy_role in roles, roles
    assert default not in roles, roles
