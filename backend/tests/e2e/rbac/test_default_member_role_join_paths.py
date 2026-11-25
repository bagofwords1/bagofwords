"""The org's default member role (Members page) reaches a new member through
**every** way a person can join an organization — not only the Add-member modal.

Regression for the report "the default-role assignment is not working": an
admin set the default role to one of the org's custom roles, yet a user who
joined through SSO landed with the built-in ``member`` role. Only
``organization_service.add_member`` read ``default_member_role``; SAML, SCIM,
LDAP, OIDC group sync, OIDC org mapping, the CSV import, domain signup and chat
auto-provision each hardcoded ``"member"`` (or read the separate sign-up
``auto_invite_role``).

Explicit choices are not "nobody chose": an admin-picked role, a sign-up
override, an OIDC org-mapping rule's role (operator config), and an embedded
host application's role all win over the default — covered below, along with
the default being writable only through its full-admin endpoint.

The invariant under test: whatever the join path, a person nobody chose a role
for ends up holding the org's default role as a real RBAC assignment, with its
permissions. Identity providers (SAML assertion, SCIM client, LDAP directory,
OIDC claims) are the boundary, so those paths are driven at the service entry
the route/callback calls after it has verified the identity.
"""
import io
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.settings.config import settings as bow_settings

# A permission the unassigned baseline does not grant, so holding it proves the
# custom role's permissions — not just its name — reached the user.
CUSTOM_PERMISSION = "create_reports"
PASSWORD = "Test1234!"


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _xlsx(emails):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["email", "note"])
    for e in emails:
        ws.append([e, "imported"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def single_org(monkeypatch):
    """The org admin owns the sign-up policy only on a single-org instance."""
    monkeypatch.setattr(bow_settings.bow_config.features, "allow_multiple_organizations", False)


@pytest.fixture
def domain_signup_licensed(enterprise_license, monkeypatch):
    from app.ee import license as ee_license

    base = ee_license.get_license_info()
    licensed = base.model_copy(update={"features": [*(base.features or []), "domain_signup"]})
    monkeypatch.setattr(ee_license, "get_license_info", lambda force_refresh=False: licensed)
    monkeypatch.setattr(ee_license, "_cached_license", licensed)
    return licensed


@pytest.fixture
def org_with_custom_default(test_client, bootstrap_admin, create_role):
    """A fresh org whose default member role is one of its own custom roles."""

    def _make(*, permissions=(CUSTOM_PERMISSION, "view_reports")):
        admin = bootstrap_admin()
        name = f"default_access_{uuid.uuid4().hex[:8]}"
        resp = create_role(name=name, permissions=list(permissions),
                           user_token=admin["token"], org_id=admin["org_id"])
        assert resp.status_code == 200, resp.text
        role = resp.json()
        put = test_client.put(
            "/api/organization/default-member-role", json={"role": name},
            headers=_headers(admin["token"], admin["org_id"]),
        )
        assert put.status_code == 200, put.text
        return SimpleNamespace(admin=admin, org_id=admin["org_id"], role=role, name=name)

    return _make


async def _new_user(email=None):
    """A user with no membership anywhere — what an IdP hands us on first sight."""
    from app.dependencies import async_session_maker
    from app.models.user import User
    from fastapi_users.password import PasswordHelper

    ph = PasswordHelper()
    email = email or f"u_{uuid.uuid4().hex[:10]}@idp.example"
    async with async_session_maker() as db:
        user = User(email=email, name=email.split("@")[0], hashed_password=ph.hash(ph.generate()),
                    is_active=True, is_verified=True, is_superuser=False)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


async def _effective(user_id, org_id):
    """(role names, org permissions) the resolver grants — what the app enforces."""
    from app.dependencies import async_session_maker
    from app.core.permission_resolver import resolve_permissions

    async with async_session_maker() as db:
        resolved = await resolve_permissions(db, str(user_id), str(org_id))
        return set(resolved.role_names), set(resolved.org_permissions)


def _register_and_login(create_user, login_user, whoami, email):
    create_user(email=email, password=PASSWORD)
    return whoami(login_user(email, PASSWORD))["id"]


# ---------------------------------------------------------------------------
# One entry per join path. Each takes the org fixture and returns the id of the
# user who just joined, having chosen no role for them.
# ---------------------------------------------------------------------------

async def _via_add_member_api(ctx, org, **_):
    email = f"added_{uuid.uuid4().hex[:8]}@test.com"
    resp = ctx.test_client.post(
        f"/api/organizations/{org.org_id}/members",
        json={"organization_id": org.org_id, "email": email},
        headers=_headers(org.admin["token"], org.org_id),
    )
    assert resp.status_code == 200, resp.text
    return _register_and_login(ctx.create_user, ctx.login_user, ctx.whoami, email)


async def _via_csv_import(ctx, org, **_):
    email = f"imported_{uuid.uuid4().hex[:8]}@test.com"
    resp = ctx.test_client.post(
        f"/api/organizations/{org.org_id}/members/import?dry_run=false",
        files={"file": ("members.xlsx", _xlsx([email]),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers=_headers(org.admin["token"], org.org_id),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["summary"]["created"] == 1
    return _register_and_login(ctx.create_user, ctx.login_user, ctx.whoami, email)


async def _via_domain_signup(ctx, org, **_):
    domain = f"{uuid.uuid4().hex[:10]}.example"
    resp = ctx.test_client.put(
        "/api/organization/signup-policy",
        # No auto_invite_role override: signups follow the org default.
        json={"enabled": True, "allowed_domains": [domain]},
        headers=_headers(org.admin["token"], org.org_id),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["auto_invite_role"] == ""
    return _register_and_login(ctx.create_user, ctx.login_user, ctx.whoami,
                               f"signup_{uuid.uuid4().hex[:8]}@{domain}")


async def _via_chat_auto_provision(ctx, org, **_):
    from app.dependencies import async_session_maker
    from app.core.auth import auto_provision_user_for_org

    domain = f"{uuid.uuid4().hex[:10]}.example"
    resp = ctx.test_client.put(
        "/api/organization/signup-policy",
        json={"enabled": True, "allowed_domains": [domain]},
        headers=_headers(org.admin["token"], org.org_id),
    )
    assert resp.status_code == 200, resp.text
    async with async_session_maker() as db:
        user = await auto_provision_user_for_org(db, org.org_id, f"chat_{uuid.uuid4().hex[:8]}@{domain}")
    assert user is not None
    return user.id


async def _via_saml_first_login(ctx, org, **_):
    from app.dependencies import async_session_maker
    from app.services.saml_service import admit_user

    cfg = SimpleNamespace(organization_id=org.org_id, auto_provision_users=True,
                          account_links={}, name="okta")
    email = f"saml_{uuid.uuid4().hex[:8]}@idp.example"
    async with async_session_maker() as db:
        user = await admit_user(cfg, "https://idp.example", f"sub-{uuid.uuid4().hex}",
                                uuid.uuid4().hex, email, "Saml User", db)
    return user.id


async def _via_scim_create(ctx, org, **_):
    from app.dependencies import async_session_maker
    from app.ee.scim.service import ScimUserService
    from app.ee.scim.schemas import ScimUserCreate, ScimEmail

    email = f"scim_{uuid.uuid4().hex[:8]}@idp.example"
    async with async_session_maker() as db:
        await ScimUserService().create_user(
            db, org.org_id,
            ScimUserCreate(userName=email, emails=[ScimEmail(value=email, primary=True)], active=True),
        )
    from app.models.user import User
    async with async_session_maker() as db:
        return (await db.execute(select(User.id).where(User.email == email))).scalar_one()


async def _via_oidc_group_sync(ctx, org, **_):
    from app.dependencies import async_session_maker
    from app.ee.oidc.group_sync_service import _ensure_org_membership

    user = await _new_user()
    async with async_session_maker() as db:
        await _ensure_org_membership(db, org.org_id, str(user.id))
        await db.commit()
    return user.id


async def _via_ldap_group_sync(ctx, org, **_):
    from app.dependencies import async_session_maker
    from app.ee.ldap.sync_service import LDAPGroupSyncService

    user = await _new_user()
    async with async_session_maker() as db:
        svc = object.__new__(LDAPGroupSyncService)  # skip the LDAP-connecting __init__
        svc.connection = SimpleNamespace(provider_id="ldap:test")
        await svc._ensure_org_memberships(db, org.org_id, {str(user.id)})
        await db.commit()
    return user.id


async def _via_ldap_first_login(ctx, org, **_):
    from app.dependencies import async_session_maker
    from app.core.auth import admit_directory_identity

    email = f"ldap_{uuid.uuid4().hex[:8]}@corp.example"
    identity = {"email": email, "name": "Ldap User", "dn": f"CN={email},DC=corp",
                "provider": "ldap:corp", "guid": uuid.uuid4().hex}
    async with async_session_maker() as db:
        user = await admit_directory_identity(
            db, identity, SimpleNamespace(auto_provision_users=True, organization_id=org.org_id),
        )
        assert user is not None
        await db.commit()
        return user.id


JOIN_PATHS = {
    "add_member_api": _via_add_member_api,
    "csv_import": _via_csv_import,
    "domain_signup": _via_domain_signup,
    "chat_auto_provision": _via_chat_auto_provision,
    "saml_first_login": _via_saml_first_login,
    "scim_create": _via_scim_create,
    "oidc_group_sync": _via_oidc_group_sync,
    "ldap_group_sync": _via_ldap_group_sync,
    "ldap_first_login": _via_ldap_first_login,
}


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(JOIN_PATHS))
async def test_default_member_role_reaches_every_join_path(
    path, test_client, domain_signup_licensed, single_org, org_with_custom_default,
    create_user, login_user, whoami,
):
    org = org_with_custom_default()
    ctx = SimpleNamespace(test_client=test_client, create_user=create_user,
                          login_user=login_user, whoami=whoami)

    user_id = await JOIN_PATHS[path](ctx, org)

    roles, perms = await _effective(user_id, org.org_id)
    assert org.name in roles, f"{path}: default role not granted; user has roles {sorted(roles)}"
    assert CUSTOM_PERMISSION in perms, f"{path}: default role's permissions not granted"
    assert "member" not in roles, f"{path}: built-in member granted instead of the default"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_builtin_default_still_gives_member(
    test_client, domain_signup_licensed, single_org, bootstrap_admin,
):
    """Control: with no default configured, every path still grants ``member``."""
    admin = bootstrap_admin()
    org = SimpleNamespace(admin=admin, org_id=admin["org_id"])
    for path in ("saml_first_login", "scim_create", "oidc_group_sync", "ldap_first_login"):
        user_id = await JOIN_PATHS[path](None, org)
        roles, _ = await _effective(user_id, org.org_id)
        assert roles == {"member"}, f"{path}: {sorted(roles)}"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_explicit_role_choices_beat_the_default(
    test_client, domain_signup_licensed, single_org, org_with_custom_default, get_system_role,
    create_user, login_user, whoami,
):
    org = org_with_custom_default()
    headers = _headers(org.admin["token"], org.org_id)

    # 1. An admin who picks a role in the Add-member modal gets exactly that.
    email = f"explicit_{uuid.uuid4().hex[:8]}@test.com"
    resp = test_client.post(f"/api/organizations/{org.org_id}/members",
                            json={"organization_id": org.org_id, "email": email, "role": "member"},
                            headers=headers)
    assert resp.status_code == 200, resp.text
    uid = _register_and_login(create_user, login_user, whoami, email)
    roles, _ = await _effective(uid, org.org_id)
    assert "member" in roles and org.name not in roles

    # 2. A sign-up policy override wins over the org default for domain signups.
    domain = f"{uuid.uuid4().hex[:10]}.example"
    resp = test_client.put("/api/organization/signup-policy",
                           json={"enabled": True, "allowed_domains": [domain], "auto_invite_role": "member"},
                           headers=headers)
    assert resp.status_code == 200 and resp.json()["auto_invite_role"] == "member"
    uid = _register_and_login(create_user, login_user, whoami, f"over_{uuid.uuid4().hex[:8]}@{domain}")
    roles, _ = await _effective(uid, org.org_id)
    assert "member" in roles and org.name not in roles


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_oidc_mapping_rule_role_is_honored_not_replaced_by_default(
    test_client, domain_signup_licensed, single_org, org_with_custom_default,
):
    """An operator's mapping rule names its role explicitly: a ``member`` rule
    gives the built-in member even when the org default is something else."""
    from app.dependencies import async_session_maker
    from app.services.oidc_org_mapping import _ensure_membership

    org = org_with_custom_default()
    user = await _new_user()
    async with async_session_maker() as db:
        await _ensure_membership(db, "okta", user, org.org_id, "member")
        await db.commit()
    roles, _ = await _effective(user.id, org.org_id)
    assert roles == {"member"}, sorted(roles)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_oidc_mapping_keeps_custom_role_on_later_sign_ins(
    test_client, domain_signup_licensed, single_org, org_with_custom_default,
):
    """Someone who already holds a custom role (here: the default, from group
    sync) is not "raised" to built-in member by a later ``member`` rule — it is
    the same tier; an ``admin`` rule still raises them."""
    from app.dependencies import async_session_maker
    from app.services.oidc_org_mapping import _ensure_membership

    org = org_with_custom_default()
    user_id = await JOIN_PATHS["oidc_group_sync"](None, org)
    async with async_session_maker() as db:
        from app.models.user import User
        user = await db.get(User, str(user_id))
    async with async_session_maker() as db:
        await _ensure_membership(db, "okta", user, org.org_id, "member")
        await db.commit()
    roles, _ = await _effective(user_id, org.org_id)
    assert org.name in roles and "member" not in roles, sorted(roles)

    async with async_session_maker() as db:
        await _ensure_membership(db, "okta", user, org.org_id, "admin")
        await db.commit()
    roles, _ = await _effective(user_id, org.org_id)
    assert "admin" in roles, sorted(roles)


@pytest.mark.e2e
@pytest.mark.parametrize("key", ["default_member_role", "signup_policy"])
def test_join_role_settings_are_not_writable_through_generic_settings(
    key, test_client, enterprise_license, org_with_custom_default, create_role,
    create_user, login_user,
):
    """The default member role decides what every future member gets (it may
    be admin), so only its full-admin endpoint may write it. A settings manager
    without full admin is refused on the generic writer — and so is an admin."""
    org = org_with_custom_default()
    mgr_role = f"settings_mgr_{uuid.uuid4().hex[:6]}"
    assert create_role(name=mgr_role, permissions=["manage_settings"],
                       user_token=org.admin["token"], org_id=org.org_id).status_code == 200
    email = f"mgr_{uuid.uuid4().hex[:8]}@test.com"
    r = test_client.post(f"/api/organizations/{org.org_id}/members",
                         json={"organization_id": org.org_id, "email": email, "role": mgr_role},
                         headers=_headers(org.admin["token"], org.org_id))
    assert r.status_code == 200, r.text
    create_user(email=email, password=PASSWORD)
    mgr_token = login_user(email, PASSWORD)

    value = "admin" if key == "default_member_role" else {"enabled": True, "allowed_domains": ["x.example"], "auto_invite_role": "admin"}
    for token in (mgr_token, org.admin["token"]):
        r = test_client.put("/api/organization/settings", json={"config": {key: value}},
                            headers=_headers(token, org.org_id))
        assert r.status_code == 403, r.text

    # The manager can't use the dedicated endpoint either; the default is intact.
    r = test_client.put("/api/organization/default-member-role", json={"role": "admin"},
                        headers=_headers(mgr_token, org.org_id))
    assert r.status_code == 403, r.text
    r = test_client.get("/api/organization/default-member-role", headers=_headers(org.admin["token"], org.org_id))
    assert r.json()["role"] == org.name


@pytest.mark.e2e
def test_customized_member_role_can_be_chosen_in_both_settings(
    test_client, domain_signup_licensed, single_org, org_with_custom_default, get_system_role,
):
    """After an admin edits the built-in Member role (an org-scoped override
    row now shares its name), picking ``member`` must save, not 500."""
    org = org_with_custom_default()
    headers = _headers(org.admin["token"], org.org_id)
    member = get_system_role("member", user_token=org.admin["token"], org_id=org.org_id)
    r = test_client.put(f"/api/organizations/{org.org_id}/roles/{member['id']}",
                        json={"permissions": ["view_reports", "create_reports"]}, headers=headers)
    assert r.status_code == 200, r.text

    r = test_client.put("/api/organization/signup-policy",
                        json={"enabled": True, "allowed_domains": [f"{uuid.uuid4().hex[:8]}.example"],
                              "auto_invite_role": "member"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["auto_invite_role"] == "member"
    r = test_client.put("/api/organization/default-member-role", json={"role": "member"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "member"


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("host_role", ["app_user", "member"])
@pytest.mark.parametrize("path", ["saml_first_login", "ldap_first_login"])
async def test_embedded_sign_in_keeps_the_host_applications_role(
    path, host_role, test_client, enterprise_license, org_with_custom_default,
):
    """Inside a host application's embedded sign-in window, a new account gets
    the role the host gives new people, never the (possibly broader) default."""
    from app.dependencies import async_session_maker
    from app.models.host_application import HostApplication
    from app.models.membership import Membership
    from app.core.workspace_access import intent_for, signup_intent_scope

    org = org_with_custom_default()
    r = test_client.post("/api/host_applications",
                         json={"name": f"Portal {uuid.uuid4().hex[:6]}", "allowed_origins": ["https://erp.example"],
                               "identity_mode": "sso", "new_user_role": host_role},
                         headers=_headers(org.admin["token"], org.org_id))
    assert r.status_code == 201, r.text
    async with async_session_maker() as db:
        host = (await db.execute(select(HostApplication).where(
            HostApplication.organization_id == org.org_id))).scalars().first()
        intent = intent_for(host)

    async with signup_intent_scope(intent):
        user_id = await JOIN_PATHS[path](None, org)

    roles, _ = await _effective(user_id, org.org_id)
    assert roles == {host_role}, f"{path}: {sorted(roles)}"
    async with async_session_maker() as db:
        m = (await db.execute(select(Membership).where(
            Membership.user_id == str(user_id), Membership.organization_id == org.org_id))).scalar_one()
        assert m.created_via_host_application_id == str(host.id)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_deleted_default_role_falls_back_to_member(
    test_client, domain_signup_licensed, single_org, org_with_custom_default,
):
    """A default that no longer resolves must not provision someone with no role."""
    org = org_with_custom_default()
    resp = test_client.delete(f"/api/organizations/{org.org_id}/roles/{org.role['id']}",
                              headers=_headers(org.admin["token"], org.org_id))
    assert resp.status_code in (200, 204), resp.text

    user_id = await JOIN_PATHS["scim_create"](None, org)
    roles, _ = await _effective(user_id, org.org_id)
    assert roles == {"member"}, sorted(roles)
