"""Token exchange: a verified Entra identity that already has an account may be
admitted into a further organization, under that organization's own rules.

Contract:
* The identity is recognised by its immutable tenant/object id link. With
  several organizations enabled, it joins the application's organization when
  that organization invited the account's email (or its domain policy admits it).
* No invitation and no domain policy: refused. Single-organization
  deployments: refused, as before. A disabled account: refused.
* An email match without the identity link is still refused.
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.membership import Membership
from app.models.organization import Organization
from app.models.user import User
from tests.e2e.org_isolation.identity_helpers import feature
from tests.e2e.test_entra_token_exchange import entra, exchange, setup_app  # noqa: F401


def _second_organization_with_app(test_client, create_user, login_user, whoami, entra):
    """Another organization, its admin, and its own exchange application.

    The organization and its admin's membership are written directly: creating
    a further organization is an instance-admin operation with its own tests."""
    cfg, external, _, _ = entra
    with feature("allow_uninvited_signups", True):
        admin = create_user(email=f"b_admin_{uuid.uuid4().hex[:8]}@test.com")
    token = login_user(admin["email"], admin["password"])
    admin_id = whoami(token)["id"]

    async def seed():
        from app.core.permission_resolver import ensure_system_role_assignment

        async with async_session_maker() as db:
            org = Organization(name=f"Org {uuid.uuid4().hex[:8]}")
            db.add(org)
            await db.flush()
            db.add(Membership(user_id=admin_id, organization_id=org.id, role="admin"))
            await ensure_system_role_assignment(db, str(org.id), admin_id, "admin")
            await db.commit()
            return str(org.id)

    org_id = asyncio.run(seed())
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}
    created = test_client.post(
        "/api/oauth/clients",
        headers=headers,
        json={
            "name": "External test B",
            "scopes": "app",
            "redirect_uris": ["http://localhost/callback"],
            "entra_exchange": {"provider": cfg.name, "external_client_id": external},
        },
    )
    assert created.status_code == 200, created.text
    return created.json(), headers, org_id


def _invite(test_client, headers, org_id, email, role):
    resp = test_client.post(
        f"/api/organizations/{org_id}/members", headers=headers,
        json={"organization_id": org_id, "email": email, "role": role},
    )
    assert resp.status_code == 200, resp.text


def _org_roles(test_client, access_token):
    me = test_client.get("/api/users/whoami", headers={"Authorization": f"Bearer {access_token}"})
    assert me.status_code == 200, me.text
    return me.json()["id"], {o["id"]: o["role"] for o in me.json()["organizations"]}


@pytest.mark.e2e
@pytest.mark.parametrize("role", ["member", "admin"])
def test_linked_identity_joins_a_second_organization_that_invited_it(
    test_client, create_user, login_user, whoami, entra, role
):
    with feature("allow_multiple_organizations", True):
        _, first_headers, info = setup_app(test_client, create_user, login_user, whoami, entra)
        app_b, headers_b, org_b = _second_organization_with_app(test_client, create_user, login_user, whoami, entra)
        token = entra[3]()

        # Not invited, no domain policy: the second organization stays closed.
        assert exchange(test_client, app_b, token).json()["error"] == "invalid_grant"

        _invite(test_client, headers_b, org_b, info["email"], role)
        admitted = exchange(test_client, app_b, token)
        assert admitted.status_code == 200, admitted.text

        user_id, roles = _org_roles(test_client, admitted.json()["access_token"])
        assert user_id == info["id"]
        assert roles[org_b] == role
        assert first_headers["X-Organization-Id"] in roles
        # One membership, and the invitation is used up.
        members = test_client.get(f"/api/organizations/{org_b}/members", headers=headers_b).json()
        assert len([m for m in members if m.get("user_id") == info["id"]]) == 1
        assert not [m for m in members if (m.get("email") or "").lower() == info["email"].lower() and not m.get("user_id")]
        assert exchange(test_client, app_b, token).status_code == 200


@pytest.mark.e2e
def test_email_match_without_the_identity_link_is_still_refused(test_client, create_user, login_user, whoami, entra):
    with feature("allow_multiple_organizations", True):
        _, _, info = setup_app(test_client, create_user, login_user, whoami, entra)
        app_b, headers_b, org_b = _second_organization_with_app(test_client, create_user, login_user, whoami, entra)
        _invite(test_client, headers_b, org_b, info["email"], "member")

        other_identity = entra[3](oid=str(uuid.uuid4()), preferred_username=info["email"], email=info["email"])

        assert exchange(test_client, app_b, other_identity).json()["error"] == "invalid_grant"
        # Being added attached the account directly; the refused exchange
        # leaves that single membership as it was.
        members = test_client.get(f"/api/organizations/{org_b}/members", headers=headers_b).json()
        assert len([m for m in members if m.get("user_id") == info["id"]]) == 1


@pytest.mark.e2e
def test_disabled_account_is_not_admitted_anywhere(test_client, create_user, login_user, whoami, entra):
    with feature("allow_multiple_organizations", True):
        _, _, info = setup_app(test_client, create_user, login_user, whoami, entra)
        app_b, headers_b, org_b = _second_organization_with_app(test_client, create_user, login_user, whoami, entra)
        _invite(test_client, headers_b, org_b, info["email"], "member")

        async def disable():
            # A deprovisioned account: no API disables the instance's first admin.
            async with async_session_maker() as db:
                user = await db.get(User, info["id"])
                user.is_active = False
                await db.commit()

        asyncio.run(disable())

        assert exchange(test_client, app_b, entra[3]()).json()["error"] == "invalid_grant"
        # Being added attached the account directly; the refused exchange
        # leaves that single membership as it was.
        members = test_client.get(f"/api/organizations/{org_b}/members", headers=headers_b).json()
        assert len([m for m in members if m.get("user_id") == info["id"]]) == 1


@pytest.mark.e2e
@pytest.mark.parametrize("several_organizations", [False, True])
def test_domain_policy_admits_a_linked_identity_only_with_several_organizations(
    test_client, create_user, login_user, whoami, entra, several_organizations, monkeypatch
):
    """A linked account without a membership here: a single-organization
    deployment keeps refusing it (unchanged); with several organizations the
    organization's domain policy decides, as it does for a new person."""
    from sqlalchemy import select
    from app.models.oauth_account import OAuthAccount
    from tests.e2e.test_seat_cap_autoprovision import _set_signup_policy

    import app.ee.license as license_module

    # Domain sign-up is a licensed feature.
    monkeypatch.setattr(license_module, "has_feature", lambda feature: True)
    app, headers, info = setup_app(test_client, create_user, login_user, whoami, entra)
    org_id = headers["X-Organization-Id"]
    member_email = f"member_{uuid.uuid4().hex[:8]}@allowed.example"
    _invite(test_client, headers, org_id, member_email, "member")
    member = create_user(email=member_email)
    member_id = whoami(login_user(member["email"], member["password"]))["id"]

    async def arrange():
        # The Entra identity belongs to the member (SSO state the local sign-up
        # API cannot produce) and the member's membership here has ended.
        async with async_session_maker() as db:
            account = await db.scalar(select(OAuthAccount).where(OAuthAccount.user_id == info["id"]))
            account.user_id = member_id
            await db.delete(await db.scalar(select(Membership).where(Membership.user_id == member_id)))
            await _set_signup_policy(db, org_id, ["allowed.example"])
            await db.commit()

    asyncio.run(arrange())

    with feature("allow_multiple_organizations", several_organizations):
        result = exchange(test_client, app, entra[3]())

    if several_organizations:
        assert result.status_code == 200, result.text
        assert _org_roles(test_client, result.json()["access_token"]) == (member_id, {org_id: "member"})
    else:
        assert result.json()["error"] == "invalid_grant"
