"""A re-added member's stored results are purged when their invite is claimed.

Accepting an invite moves its groups onto the account. Groups feed the row
filters compiled for a viewer, so the results the account stored in that
organization before it was removed are outdated by the claim: once the claim
has committed they are purged (app/services/viewer_result_freshness
.purge_members_results), as when an invite attaches an account directly
(tests/e2e/rbac/test_invite_roles_and_groups.py).

``/api/invites/accept`` is covered there; this file covers the claims made on
the account's behalf: an SSO sign-in, SCIM provisioning, OIDC group sync and
chat onboarding. A sign-in or a directory push waits on its purge, so it gets
the short budget.
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from tests.e2e.org_isolation.identity_helpers import FakeIdentityProvider, oidc_provider
from tests.fixtures.member_purge_spy import member_purges  # noqa: F401 (fixture)


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _invite(test_client, admin, email, group_ids=()):
    resp = test_client.post(
        f"/api/organizations/{admin['org_id']}/members",
        json={"organization_id": admin["org_id"], "email": email, "role": "member", "group_ids": list(group_ids)},
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _accept(test_client, admin, membership_id, user_token):
    link = test_client.get(f"/api/organizations/{admin['org_id']}/members/{membership_id}/invite-link",
                           headers=_hdr(admin["token"], admin["org_id"]))
    assert link.status_code == 200, link.text
    resp = test_client.post("/api/invites/accept", json={"token": link.json()["token"]},
                            headers={"Authorization": f"Bearer {user_token}"})
    assert resp.status_code == 200, resp.text


def _remove(test_client, admin, membership_id):
    resp = test_client.delete(f"/api/organizations/{admin['org_id']}/members/{membership_id}",
                              headers=_hdr(admin["token"], admin["org_id"]))
    assert resp.status_code == 204, resp.text


@pytest.fixture
def org(enterprise_license, bootstrap_admin, create_group):
    admin = bootstrap_admin()
    groups = []
    for _ in range(2):
        g = create_group(name=f"team-{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=admin["org_id"])
        assert g.status_code == 200, g.text
        groups.append(g.json()["id"])
    return {**admin, "groups": groups}


@pytest.fixture
def invited_back(org, test_client, bootstrap_admin):
    """Someone who belongs to another organization was a member here (by an
    accepted invite), was removed, and is invited back into `org`'s groups:
    the invite is pending (an account elsewhere is never attached directly)."""

    def _make(while_member=None):
        other = bootstrap_admin("elsewhere")
        first = _invite(test_client, org, other["email"])
        _accept(test_client, org, first["id"], other["token"])
        if while_member:
            while_member(other)
        _remove(test_client, org, first["id"])
        again = _invite(test_client, org, other["email"], org["groups"])
        assert again["user_id"] is None
        return other

    return _make


def _assert_one_quick_purge(member_purges, org, user_id):
    import app.services.viewer_result_freshness as freshness

    assert [c["user_ids"] for c in member_purges] == [[user_id]]
    assert member_purges[0]["organization_id"] == org["org_id"]
    assert set(org["groups"]) <= member_purges[0]["groups"][user_id]  # committed
    assert member_purges[0]["budget_seconds"] == freshness.QUICK_PURGE_BUDGET_SECONDS


@pytest.mark.e2e
def test_an_sso_sign_in_that_claims_the_invite_purges_the_members_results(
        org, invited_back, test_client, create_group, monkeypatch, member_purges):
    """The first SSO sign-in links the account by its email and claims the
    invite; a later one (the account already linked) claims the next."""
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider()])
    other = invited_back()
    assert member_purges == []

    outcome, _ = idp.sign_in(test_client, "corp", other["email"])
    assert outcome == "ok"
    _assert_one_quick_purge(member_purges, org, other["user_id"])

    member_purges.clear()
    membership = next(m for m in test_client.get(
        f"/api/organizations/{org['org_id']}/members", headers=_hdr(org["token"], org["org_id"])).json()
        if m.get("user_id") == other["user_id"])
    _remove(test_client, org, membership["id"])
    new = create_group(name=f"team-{uuid.uuid4().hex[:6]}", user_token=org["token"], org_id=org["org_id"])
    assert new.status_code == 200, new.text
    wider = _invite(test_client, org, other["email"], [new.json()["id"]])
    assert wider["user_id"] is None
    outcome, _ = idp.sign_in(test_client, "corp", other["email"])
    assert outcome == "ok"
    assert [c["user_ids"] for c in member_purges] == [[other["user_id"]]]
    assert new.json()["id"] in member_purges[0]["groups"][other["user_id"]]


@pytest.mark.e2e
def test_an_org_mapping_that_claims_the_invite_purges_the_members_results(
        org, invited_back, test_client, monkeypatch, member_purges):
    """A provider not trusted to link by email claims no invite at sign-in;
    the operator's org_mapping placing the account in `org` claims it."""
    sales = str(uuid.uuid4())
    other = invited_back()
    # Their SSO identity, linked by an earlier sign-in (the provider trusted
    # then), which claimed that invite.
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider()])
    assert idp.sign_in(test_client, "corp", other["email"])[0] == "ok"
    membership = next(m for m in test_client.get(
        f"/api/organizations/{org['org_id']}/members", headers=_hdr(org["token"], org["org_id"])).json()
        if m.get("user_id") == other["user_id"])
    _remove(test_client, org, membership["id"])
    _invite(test_client, org, other["email"], org["groups"])
    member_purges.clear()

    from app.settings.config import settings as bow_settings

    monkeypatch.setattr(bow_settings.bow_config, "oidc_providers", [oidc_provider(
        trust_email_for_linking=False, org_mapping=[f"{sales}:{org['org_id']}:member"])])
    outcome, _ = idp.sign_in(test_client, "corp", other["email"], groups=[sales])
    assert outcome == "ok"
    _assert_one_quick_purge(member_purges, org, other["user_id"])


@pytest.mark.e2e
def test_chat_onboarding_that_claims_the_invite_purges_the_members_results(org, invited_back, member_purges):
    from app.core.auth import auto_provision_user_for_org

    other = invited_back()

    async def onboard():
        async with async_session_maker() as db:
            return await auto_provision_user_for_org(db, org["org_id"], other["email"])

    assert asyncio.run(onboard()) is not None
    _assert_one_quick_purge(member_purges, org, other["user_id"])


@pytest.mark.e2e
def test_an_oidc_group_sync_that_claims_the_invite_purges_the_members_results(org, invited_back, member_purges):
    """The sync claims the invite and adds the sign-in's directory groups
    (removal ended the ones they held here): one purge, after both."""
    from app.ee.oidc.group_sync_service import sync_user_oidc_groups

    other = invited_back()

    async def sync():
        async with async_session_maker() as db:
            await sync_user_oidc_groups(db=db, user_id=other["user_id"], organization_id=org["org_id"],
                                        group_ids=["claim-aaa"])

    asyncio.run(sync())
    _assert_one_quick_purge(member_purges, org, other["user_id"])


def _keep_groups(user_id, group_ids):
    """The account holds these groups without a membership here: what a
    member removed before removal ended their groups still holds (and an
    LDAP sync's seat-cap skip leaves). Written directly: no public path
    produces it today."""
    from app.models.group_membership import GroupMembership

    async def run():
        async with async_session_maker() as db:
            db.add_all([GroupMembership(group_id=g, user_id=str(user_id)) for g in group_ids])
            await db.commit()

    asyncio.run(run())


def _directory_group_ids(org_id, provider):
    from sqlalchemy import select

    from app.models.group import Group

    async def run():
        async with async_session_maker() as db:
            return [str(g) for g in (await db.scalars(select(Group.id).where(
                Group.organization_id == org_id, Group.external_provider == provider,
                Group.deleted_at.is_(None)))).all()]

    return asyncio.run(run())


@pytest.mark.e2e
def test_an_oidc_group_sync_with_no_directory_change_still_purges_a_claimed_invite(
        org, invited_back, member_purges):
    """The member still holds the sign-in's directory group (legacy rows the
    removal left): the directory diff is empty, but the claimed invite gives
    them `org`'s groups, so they are purged all the same, once."""
    from app.ee.oidc.group_sync_service import sync_user_oidc_groups

    async def sync(user_id):
        async with async_session_maker() as db:
            await sync_user_oidc_groups(db=db, user_id=user_id, organization_id=org["org_id"],
                                        group_ids=["claim-kept"])

    other = invited_back(while_member=lambda o: asyncio.run(sync(o["user_id"])))
    _keep_groups(other["user_id"], _directory_group_ids(org["org_id"], "oidc"))
    member_purges.clear()

    asyncio.run(sync(other["user_id"]))
    _assert_one_quick_purge(member_purges, org, other["user_id"])


@pytest.mark.e2e
def test_an_ldap_sync_that_claims_the_invite_purges_the_members_results(
        org, invited_back, monkeypatch, member_purges):
    """The member still holds their directory group (legacy rows the removal
    left), so the sync changes none of their directory groups; it claims
    their pending invite into `org`'s groups: they are purged, in the sync's
    one purge, after it committed."""
    from app.ee.ldap.connection import LDAPConnectionManager
    from app.ee.ldap.sync_service import LDAPGroupSyncService
    from app.models.user import User
    from app.settings.bow_config import LDAPConfig

    config = LDAPConfig(organization_id=org["org_id"])
    provider = LDAPConnectionManager(config).provider_id
    seen = {}

    def bind(other):
        dn = f"cn={uuid.uuid4().hex[:8]},ou=Users,dc=test,dc=com"
        identity = {"provider": provider, "guid": str(uuid.uuid4()), "sid_hex": "01", "dn": dn,
                    "email": other["email"]}
        seen.update(dn=dn, identity=identity)

        async def run():
            async with async_session_maker() as db:
                user = await db.get(User, other["user_id"])
                user.ldap_identity = identity
                user.ldap_subject = f"{provider}:{identity['guid']}"
                await db.commit()

        asyncio.run(run())
        monkeypatch.setattr(LDAPConnectionManager, "read_identity", lambda self, ref: dict(identity))
        monkeypatch.setattr(LDAPConnectionManager, "search_users",
                            lambda *a: [{"dn": dn, "email": other["email"]}])
        monkeypatch.setattr(LDAPConnectionManager, "search_groups", lambda *a: [
            {"dn": "cn=Sales,ou=Groups,dc=test,dc=com", "name": f"Sales {uuid.uuid4().hex[:6]}", "members": [dn]}])
        sync()

    async def _sync():
        async with async_session_maker() as db:
            result = await LDAPGroupSyncService(config).sync_groups(db, org["org_id"])
            assert not result.errors, result.errors
            return result

    def sync():
        return asyncio.run(_sync())

    other = invited_back(while_member=bind)
    _keep_groups(other["user_id"], _directory_group_ids(org["org_id"], "ldap"))
    member_purges.clear()

    result = sync()
    assert (result.memberships_added, result.memberships_removed) == (0, 0)
    assert [c["user_ids"] for c in member_purges] == [[other["user_id"]]]
    assert member_purges[0]["organization_id"] == org["org_id"]
    assert set(org["groups"]) <= member_purges[0]["groups"][other["user_id"]]  # committed

    sync()  # the claim's notes were consumed: nothing more
    assert len(member_purges) == 1


@pytest.mark.e2e
def test_an_org_mapping_claim_rolled_back_purges_nothing(
        org, invited_back, test_client, monkeypatch, member_purges):
    """Placing the account in `org` fails after it claimed the invite: the
    claim is rolled back, so nothing it noted is purged."""
    from app.core import user_lifecycle
    from app.settings.config import settings as bow_settings

    sales = str(uuid.uuid4())
    other = invited_back()
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider()])
    assert idp.sign_in(test_client, "corp", other["email"])[0] == "ok"
    membership = next(m for m in test_client.get(
        f"/api/organizations/{org['org_id']}/members", headers=_hdr(org["token"], org["org_id"])).json()
        if m.get("user_id") == other["user_id"])
    _remove(test_client, org, membership["id"])
    _invite(test_client, org, other["email"], org["groups"])
    member_purges.clear()

    real = user_lifecycle.reactivate_user_for_membership

    async def fails_here(db, user_id, organization_id):
        if str(organization_id) == org["org_id"]:
            raise RuntimeError("placement failed")
        return await real(db, user_id, organization_id)

    monkeypatch.setattr(user_lifecycle, "reactivate_user_for_membership", fails_here)
    monkeypatch.setattr(bow_settings.bow_config, "oidc_providers", [oidc_provider(
        trust_email_for_linking=False, org_mapping=[f"{sales}:{org['org_id']}:member"])])
    outcome, _ = idp.sign_in(test_client, "corp", other["email"], groups=[sales])
    assert outcome == "ok"
    assert member_purges == []


@pytest.mark.e2e
def test_scim_provisioning_that_claims_the_invite_purges_the_members_results(
        org, test_client, create_user, login_user, whoami, monkeypatch, member_purges):
    """On a single-organization deployment SCIM claims a removed member's
    pending invite for their existing account."""
    from app.settings.config import settings as bow_settings

    token = test_client.post("/api/enterprise/scim/tokens", json={"name": "scim"},
                             headers=_hdr(org["token"], org["org_id"]))
    assert token.status_code == 201, token.text
    email = f"scim_{uuid.uuid4().hex[:10]}@test.com"
    first = _invite(test_client, org, email)
    create_user(email=email, password="Test1234!")
    user_id = whoami(login_user(email, "Test1234!"))["id"]
    _remove(test_client, org, first["id"])
    again = _invite(test_client, org, email, org["groups"])
    assert again["user_id"] is None  # multiple organizations: never attached directly
    member_purges.clear()

    monkeypatch.setattr(bow_settings.bow_config.features, "allow_multiple_organizations", False)
    resp = test_client.post(
        "/scim/v2/Users",
        json={"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "userName": email, "active": True},
        headers={"Authorization": f"Bearer {token.json()['token']}"},
    )
    assert resp.status_code in (200, 201), resp.text
    _assert_one_quick_purge(member_purges, org, user_id)


@pytest.mark.e2e
def test_a_claim_purges_only_the_inviting_organizations_results(org, invited_back, test_client, monkeypatch):
    """The real purge, end to end, on an SSO sign-in: the results the member
    stored here go (their latest kept, cleared); those they hold in their
    other organization are not touched."""
    from tests.fixtures.member_results import member_results_now, purged, seed_member_results_now, untouched

    idp = FakeIdentityProvider(monkeypatch, [oidc_provider()])
    seeded = {}

    def seed(other):
        seeded["here"] = seed_member_results_now(org["org_id"], other["user_id"])
        seeded["theirs"] = seed_member_results_now(other["org_id"], other["user_id"])

    other = invited_back(while_member=seed)
    assert member_results_now(seeded["here"]) == untouched()

    outcome, _ = idp.sign_in(test_client, "corp", other["email"])
    assert outcome == "ok"
    assert member_results_now(seeded["here"]) == purged()
    assert member_results_now(seeded["theirs"]) == untouched()
