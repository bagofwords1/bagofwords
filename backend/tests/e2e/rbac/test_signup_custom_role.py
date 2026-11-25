"""The sign-up policy's ``auto_invite_role`` must reach a domain-admitted user
even when it is a **custom, org-scoped role** — not only a built-in one.

Regression for the bug where an org set ``auto_invite_role`` to one of its own
custom roles (the picker and the policy validator both accept custom roles),
yet a user who signed up through the matching domain landed with **no role at
all** and fell back to baseline access. The provisioning assignment resolved
through ``resolve_assignable_system_role``, which knows only system roles and
their overrides, so a custom role name resolved to ``None`` and was silently
dropped. ``ensure_system_role_assignment`` now resolves through
``resolve_assignable_role``, which also admits a custom org role.

Both sign-up role assignments run through the same helper, so a *system* role
(the control below) was never affected — it is here to pin the asymmetry the
bug turned on.
"""
import uuid

import pytest

from app.settings.config import settings as bow_settings


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _set_signup_policy(test_client, admin, *, domain, role):
    resp = test_client.put(
        "/api/organization/signup-policy",
        json={"enabled": True, "allowed_domains": [domain], "auto_invite_role": role},
        headers=_headers(admin["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["auto_invite_role"] == role


def _roles_for(test_client, admin, user_id):
    """The user's effective org roles and permissions, read as the admin."""
    members = test_client.get(
        f"/api/organizations/{admin['org_id']}/members",
        headers=_headers(admin["token"], admin["org_id"]),
    ).json()
    me = next(m for m in members if (m.get("user") or {}).get("id") == user_id)
    return {r["name"] for r in (me["roles"] or [])}


@pytest.fixture
def single_org(monkeypatch):
    """Let the org admin own the sign-up policy: assigning a domain is an
    instance-admin act only when several organizations share the instance."""
    monkeypatch.setattr(
        bow_settings.bow_config.features, "allow_multiple_organizations", False
    )


@pytest.fixture
def domain_signup_licensed(enterprise_license, monkeypatch):
    """The suite's enterprise license plus the ``domain_signup`` feature the
    policy route gates on (``has_feature`` resolves ``get_license_info`` live)."""
    from app.ee import license as ee_license

    base = ee_license.get_license_info()
    licensed = base.model_copy(
        update={"features": [*(base.features or []), "domain_signup"]}
    )
    monkeypatch.setattr(ee_license, "get_license_info", lambda force_refresh=False: licensed)
    monkeypatch.setattr(ee_license, "_cached_license", licensed)
    return licensed


@pytest.mark.e2e
def test_custom_auto_invite_role_reaches_domain_signup_user(
    test_client, domain_signup_licensed, single_org,
    bootstrap_admin, create_role, list_role_assignments,
    create_user, login_user, whoami,
):
    admin = bootstrap_admin()

    # An org's own custom role, carrying a permission the unassigned baseline
    # does not grant.
    role_name = f"analyst_{uuid.uuid4().hex[:8]}"
    created = create_role(
        name=role_name, permissions=["view_reports", "create_reports"],
        user_token=admin["token"], org_id=admin["org_id"],
    )
    assert created.status_code == 200, created.text
    custom_role = created.json()
    assert custom_role["is_system"] is False

    domain = f"{uuid.uuid4().hex[:10]}.example"
    _set_signup_policy(test_client, admin, domain=domain, role=role_name)

    # A user signs up through the admitted domain (no prior invite — the domain
    # policy mints it during registration).
    email = f"newbie_{uuid.uuid4().hex[:8]}@{domain}"
    create_user(email=email, password="Test1234!")
    token = login_user(email, "Test1234!")
    info = whoami(token)
    user_id = info["id"]
    org = next(o for o in info["organizations"] if o["id"] == admin["org_id"])

    # The custom role — and its permissions — actually reached the user.
    assert role_name in set(org["roles"]), (
        f"custom auto_invite_role was dropped; user has roles {org['roles']!r}"
    )
    assert "create_reports" in set(org["permissions"])

    # And it is a real RBAC assignment, not just the legacy label.
    assignments = list_role_assignments(
        user_token=admin["token"], org_id=admin["org_id"],
        principal_type="user", principal_id=user_id,
    )
    assert any(a["role_id"] == custom_role["id"] for a in assignments)
    assert role_name in _roles_for(test_client, admin, user_id)


@pytest.mark.e2e
def test_system_auto_invite_role_still_reaches_domain_signup_user(
    test_client, domain_signup_licensed, single_org,
    bootstrap_admin, get_system_role, list_role_assignments,
    create_user, login_user, whoami,
):
    """Control: a built-in ``auto_invite_role`` works exactly as before."""
    admin = bootstrap_admin()
    member_role = get_system_role("member", user_token=admin["token"], org_id=admin["org_id"])

    domain = f"{uuid.uuid4().hex[:10]}.example"
    _set_signup_policy(test_client, admin, domain=domain, role="member")

    email = f"member_{uuid.uuid4().hex[:8]}@{domain}"
    create_user(email=email, password="Test1234!")
    token = login_user(email, "Test1234!")
    info = whoami(token)
    org = next(o for o in info["organizations"] if o["id"] == admin["org_id"])

    assert "member" in set(org["roles"])
    assignments = list_role_assignments(
        user_token=admin["token"], org_id=admin["org_id"],
        principal_type="user", principal_id=info["id"],
    )
    assert any(a["role_id"] == member_role["id"] for a in assignments)
