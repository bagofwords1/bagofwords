"""Operator-owned placement of people into organizations by identity-provider group.

Contract (``org_mapping`` on an OIDC provider in bow-config):
* At sign-in, every rule whose group the provider asserts places the person in
  the rule's organization with the rule's role. A mapped person needs no invite.
* A membership the mapping created ends when the person holds no group mapped
  to that organization any more. Memberships from anywhere else are left alone.
* A higher mapped role is granted on a later sign-in; a role is never lowered.
* A rule naming an unknown organization, or a full organization, is skipped
  without failing the sign-in. Groups that cannot be determined change nothing.
* Group and profile sync run in every organization the person belongs to.
* With no mapping configured, sign-in behaves as before.

Only the identity provider is replaced (see ``identity_helpers``).
"""
import asyncio
import uuid

import httpx
import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from tests.e2e.org_isolation.identity_helpers import (
    FakeIdentityProvider, bootstrap_admin, feature, hdr, invite, oidc_provider, unique_email, whoami,
)

G_SALES = "5d1f8a52-0c11-4d6f-9a3e-6f0f6f1a0001"
G_FINANCE = "5d1f8a52-0c11-4d6f-9a3e-6f0f6f1a0002"
G_FINANCE_LEADS = "5d1f8a52-0c11-4d6f-9a3e-6f0f6f1a0003"


@pytest.fixture
def multi_org():
    with feature("allow_multiple_organizations", True):
        yield


def _second_organization():
    """An organization other than the bootstrap admin's.

    Written directly: creating a further organization is an instance-admin
    operation with its own tests, and here only its existence matters."""
    name = f"Org {uuid.uuid4().hex[:8]}"

    async def _create():
        async with async_session_maker() as db:
            org = Organization(name=name)
            db.add(org)
            await db.commit()
            return str(org.id)

    return {"id": asyncio.run(_create()), "name": name}


def _roles(client, session):
    """organization id -> role, as the signed-in person sees it."""
    return {o["id"]: o["role"] for o in whoami(client, session)["organizations"]}


def _sign_in(idp, client, email, groups, provider="corp", **claims):
    outcome, session = idp.sign_in(client, provider, email, groups=groups, **claims)
    assert outcome == "ok", session
    return session


def _groups(client, session, org_id):
    resp = client.get(f"/api/organizations/{org_id}/groups", headers=hdr(session, org_id))
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("form", ["objects", "compact"])
def test_first_sign_in_places_the_person_in_every_mapped_organization(test_client, monkeypatch, multi_org, form):
    admin = bootstrap_admin(test_client)
    other = _second_organization()
    rules = {
        "objects": [
            {"group": G_SALES, "organization": admin["org_id"], "role": "member"},
            {"group": G_FINANCE, "organization": other["name"], "role": "admin"},
        ],
        "compact": [f"{G_SALES}:{admin['org_id']}:member", f"{G_FINANCE}:{other['id']}:admin"],
    }[form]
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=rules)])

    session = _sign_in(idp, test_client, unique_email("mapped"), [G_FINANCE, G_SALES, str(uuid.uuid4())])

    assert _roles(test_client, session) == {admin["org_id"]: "member", other["id"]: "admin"}
    # The roles are real: admin-only surface opens in one organization only.
    assert test_client.get(f"/api/organizations/{other['id']}/members", headers=hdr(session, other["id"])).status_code == 200
    assert test_client.post(
        f"/api/organizations/{admin['org_id']}/members",
        json={"organization_id": admin["org_id"], "email": unique_email("x"), "role": "member"},
        headers=hdr(session, admin["org_id"]),
    ).status_code == 403


def test_person_without_a_mapped_group_still_needs_an_invitation(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[f"{G_SALES}:{admin['org_id']}:member"])])

    assert idp.sign_in(test_client, "corp", unique_email("stranger"), groups=[G_FINANCE]) == ("error", "invitation_required")
    assert idp.sign_in(test_client, "corp", unique_email("nogroups")) == ("error", "invitation_required")


def test_losing_the_group_ends_only_the_membership_the_mapping_created(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    other = _second_organization()
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[
        f"{G_SALES}:{admin['org_id']}:member", f"{G_FINANCE}:{other['id']}:member",
        f"{G_FINANCE_LEADS}:{other['id']}:admin",
    ])])
    # The other organization's administrator, there through the mapping too.
    lead = unique_email("lead")
    assert _roles(test_client, _sign_in(idp, test_client, lead, [G_FINANCE_LEADS])) == {other["id"]: "admin"}
    mapped, invited = unique_email("mapped"), unique_email("invited")
    # ``invited`` is in the first organization by an admin's invite, not by the mapping.
    invite(test_client, admin["token"], admin["org_id"], invited)

    for email in (mapped, invited):
        session = _sign_in(idp, test_client, email, [G_SALES, G_FINANCE])
        assert set(_roles(test_client, session)) == {admin["org_id"], other["id"]}

    # Still in one mapped group: only the other organization's membership ends.
    assert set(_roles(test_client, _sign_in(idp, test_client, mapped, [G_SALES]))) == {admin["org_id"]}
    # In no mapped group at all.
    assert _roles(test_client, _sign_in(idp, test_client, mapped, [])) == {}
    assert _roles(test_client, _sign_in(idp, test_client, invited, [])) == {admin["org_id"]: "member"}
    # Regaining the group restores the membership.
    assert set(_roles(test_client, _sign_in(idp, test_client, mapped, [G_FINANCE]))) == {other["id"]}
    # An organization never loses its last administrator this way.
    assert _roles(test_client, _sign_in(idp, test_client, lead, [])) == {other["id"]: "admin"}


def test_removal_runs_the_member_removal_cleanup(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[f"{G_SALES}:{admin['org_id']}:member"])])
    email = unique_email("mapped")
    session = _sign_in(idp, test_client, email, [G_SALES])
    key = test_client.post(
        "/api/api_keys", json={"name": "k"}, headers=hdr(session, admin["org_id"]),
    )
    assert key.status_code == 200, key.text
    api_key = key.json()["key"]
    assert test_client.get("/api/users/whoami", headers={"Authorization": f"Bearer {api_key}"}).status_code == 200

    _sign_in(idp, test_client, email, [])

    members = test_client.get(
        f"/api/organizations/{admin['org_id']}/members", headers=hdr(admin["token"], admin["org_id"])
    ).json()
    assert email not in {(m.get("user") or {}).get("email") for m in members}
    assert test_client.get("/api/users/whoami", headers={"Authorization": f"Bearer {api_key}"}).status_code in (401, 403)


def test_higher_mapped_role_is_granted_and_never_lowered(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    org = admin["org_id"]
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[
        f"{G_FINANCE}:{org}:member", f"{G_FINANCE_LEADS}:{org}:admin",
    ])])
    email = unique_email("mapped")

    assert _roles(test_client, _sign_in(idp, test_client, email, [G_FINANCE])) == {org: "member"}
    promoted = _sign_in(idp, test_client, email, [G_FINANCE, G_FINANCE_LEADS])
    assert _roles(test_client, promoted) == {org: "admin"}
    assert test_client.get(f"/api/organizations/{org}/members", headers=hdr(promoted, org)).status_code == 200
    # Roles are only ever raised by the mapping: an admin may have set this one.
    assert _roles(test_client, _sign_in(idp, test_client, email, [G_FINANCE])) == {org: "admin"}


def test_rule_for_an_unknown_organization_is_skipped(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[
        f"{G_SALES}:{uuid.uuid4()}:admin",
        f"{G_SALES}:No Such Organization {uuid.uuid4().hex[:6]}:admin",
        f"{G_SALES}:{admin['org_id']}:member",
    ])])

    session = _sign_in(idp, test_client, unique_email("mapped"), [G_SALES])

    assert _roles(test_client, session) == {admin["org_id"]: "member"}


def test_full_organization_is_skipped_without_failing_the_sign_in(test_client, monkeypatch, multi_org):
    import app.ee.license as license_module

    admin = bootstrap_admin(test_client)
    other = _second_organization()
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[
        f"{G_SALES}:{admin['org_id']}:member", f"{G_SALES}:{other['id']}:member",
    ])])
    # One seat per organization: the first is taken by its admin, the other is empty.
    monkeypatch.setattr(license_module, "get_max_users", lambda: 1)
    email = unique_email("mapped")

    assert _roles(test_client, _sign_in(idp, test_client, email, [G_SALES])) == {other["id"]: "member"}

    monkeypatch.setattr(license_module, "get_max_users", lambda: -1)
    assert set(_roles(test_client, _sign_in(idp, test_client, email, [G_SALES]))) == {admin["org_id"], other["id"]}


def test_mapping_applies_only_to_the_provider_it_is_configured_on(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    other = _second_organization()
    idp = FakeIdentityProvider(monkeypatch, [
        oidc_provider(name="corp", org_mapping=[f"{G_SALES}:{admin['org_id']}:member"]),
        oidc_provider(name="partner", issuer="https://partner.identity-tests.example",
                      org_mapping=[f"{G_FINANCE}:{other['id']}:member"]),
    ])
    email = unique_email("mapped")

    session = _sign_in(idp, test_client, email, [G_SALES, G_FINANCE], provider="corp")

    assert set(_roles(test_client, session)) == {admin["org_id"]}


def test_groups_that_cannot_be_determined_remove_nobody(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(org_mapping=[f"{G_SALES}:{admin['org_id']}:member"])])
    email = unique_email("mapped")
    _sign_in(idp, test_client, email, [G_SALES])

    # The provider signals "too many groups to list" and the directory lookup
    # (answered 404 by the fake provider) fails.
    outcome, session = idp.sign_in(test_client, "corp", email, _claim_names={"groups": "src1"})

    assert outcome == "ok"
    assert _roles(test_client, session) == {admin["org_id"]: "member"}


class _GoogleWorkspace(FakeIdentityProvider):
    """Also answers the profile lookup made with the sign-in token."""

    profile_name = "Dana Example"

    def _respond(self, request: httpx.Request) -> httpx.Response:
        if "oauth2/v1/userinfo" in str(request.url):
            return httpx.Response(200, json={"name": self.profile_name}, request=request)
        return super()._respond(request)


def _enable_profile_sync(client, session, org_id):
    resp = client.put(
        "/api/organization/identity/google-profile-sync",
        json={"enabled": True, "fields": ["displayName"]}, headers=hdr(session, org_id),
    )
    assert resp.status_code == 200, resp.text


def _synced_profile(client, session, org_id):
    resp = client.get("/api/users/me/instructions", headers=hdr(session, org_id))
    assert resp.status_code == 200, resp.text
    return resp.json().get("profile_attributes") or {}


def test_groups_and_profile_sync_into_every_organization_of_the_person(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    first, second = admin["org_id"], _second_organization()["id"]
    idp = _GoogleWorkspace(monkeypatch, [oidc_provider(
        issuer="https://accounts.google.com", sync_groups=True, org_mapping=[f"{G_FINANCE}:{second}:admin"],
    )])
    groups = ["analysts", "finance-leads", G_FINANCE]

    # The instance's first admin, an admin of both organizations after sign-in.
    session = _sign_in(idp, test_client, admin["email"], groups)
    assert _roles(test_client, session) == {first: "admin", second: "admin"}

    for org_id in (first, second):
        synced = {g["external_id"]: g for g in _groups(test_client, session, org_id) if g["external_provider"] == "oidc"}
        assert set(synced) == set(groups)
        assert all(admin["id"] in g["member_user_ids"] for g in synced.values())

    # Profile sync is each organization's own switch.
    _enable_profile_sync(test_client, session, second)
    session = _sign_in(idp, test_client, admin["email"], groups)
    assert _synced_profile(test_client, session, second) == {"displayName": idp.profile_name}
    assert _synced_profile(test_client, session, first) == {}

    _enable_profile_sync(test_client, session, first)
    idp.profile_name = "Dana Renamed"
    session = _sign_in(idp, test_client, admin["email"], groups[:1] + [G_FINANCE])
    for org_id in (first, second):
        assert _synced_profile(test_client, session, org_id) == {"displayName": "Dana Renamed"}
        mine = {g["external_id"] for g in _groups(test_client, session, org_id) if admin["id"] in g["member_user_ids"]}
        assert mine == {"analysts", G_FINANCE}


@pytest.mark.parametrize("mapping", [[], None])
def test_without_a_mapping_sign_in_and_group_sync_are_unchanged(test_client, monkeypatch, mapping):
    admin = bootstrap_admin(test_client)
    other = _second_organization()
    options = {"sync_groups": True}
    if mapping is not None:
        options["org_mapping"] = mapping
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(**options)])
    member = unique_email("member")
    invite(test_client, admin["token"], admin["org_id"], member)

    # Uninvited people are refused whatever groups they carry.
    assert idp.sign_in(test_client, "corp", unique_email("stranger"), groups=[G_SALES]) == ("error", "invitation_required")

    session = _sign_in(idp, test_client, member, [G_SALES, other["id"], other["name"]])
    assert _roles(test_client, session) == {admin["org_id"]: "member"}
    synced = {
        g["external_id"]: g["member_user_ids"]
        for g in _groups(test_client, admin["token"], admin["org_id"]) if g["external_provider"] == "oidc"
    }
    assert set(synced) == {G_SALES, other["id"], other["name"]}
    assert all(whoami(test_client, session)["id"] in ids for ids in synced.values())

    # Leaving every group changes group membership only, never the organization.
    session = _sign_in(idp, test_client, member, [])
    assert _roles(test_client, session) == {admin["org_id"]: "member"}
