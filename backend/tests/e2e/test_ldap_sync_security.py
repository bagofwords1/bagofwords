"""A complete directory snapshot may remove only directory-owned access."""
import asyncio
import uuid
import pytest
from sqlalchemy import select
from app.ee.ldap.connection import LDAPConnectionManager
from app.ee.ldap.sync_service import LDAPGroupSyncService
from app.models.membership import Membership
from app.settings.bow_config import LDAPConfig
from tests.fixtures.member_purge_spy import member_purges  # noqa: F401 (fixture)


@pytest.mark.e2e
def test_empty_directory_preserves_invited_members(test_client, create_user, login_user, whoami, monkeypatch):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org = whoami(token)["organizations"][0]["id"]
    email = f"manual-{uuid.uuid4().hex}@example.com"
    assert test_client.post(f"/api/organizations/{org}/members",
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org},
        json={"organization_id": org, "email": email, "role": "member"}).status_code == 200
    member = create_user(email=email)
    member_id = whoami(login_user(email, member["password"]))["id"]
    monkeypatch.setattr(LDAPConnectionManager, "search_users", lambda *a: [])
    monkeypatch.setattr(LDAPConnectionManager, "search_groups", lambda *a: [])
    async def run():
        from app.dependencies import async_session_maker
        async with async_session_maker() as db:
            result = await LDAPGroupSyncService(LDAPConfig(organization_id=org)).sync_groups(db, org)
            assert not result.errors
            m = (await db.execute(select(Membership).where(Membership.user_id == member_id,
                Membership.organization_id == org))).scalar_one()
            assert m.deleted_at is None
    asyncio.run(run())


@pytest.mark.e2e
def test_ldap_sync_claims_open_invite_instead_of_duplicating(test_client, create_user, login_user, whoami):
    """An LDAP-provisioned user who already has an open email invite must have that
    invite CLAIMED, not get a second membership row.

    The invite + SSO duplicate is exactly what broke principal_belongs_to_org. A
    claimed invite stays manually owned (``directory_provider`` NULL) so directory
    cleanup never sweeps an admin-granted membership.
    """
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org = whoami(token)["organizations"][0]["id"]

    email = f"ldap-invitee-{uuid.uuid4().hex}@example.com"
    # Admin invites the user (open invite: user_id NULL, email set).
    assert test_client.post(f"/api/organizations/{org}/members",
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org},
        json={"organization_id": org, "email": email, "role": "member"}).status_code == 200
    # The same person also exists as a user (their own org) and LDAP now sees them.
    member = create_user(email=email)
    member_id = whoami(login_user(email, member["password"]))["id"]

    async def run():
        from app.dependencies import async_session_maker
        async with async_session_maker() as db:
            # Drive the LDAP provisioning path directly for this candidate.
            await LDAPGroupSyncService(LDAPConfig(organization_id=org))._ensure_org_memberships(
                db, org, {member_id}
            )
            await db.commit()
            rows = (await db.execute(select(Membership).where(
                Membership.organization_id == org,
                Membership.user_id == member_id,
                Membership.deleted_at.is_(None),
            ))).scalars().all()
            assert len(rows) == 1, f"expected one membership, got {len(rows)}"
            # Claimed, not freshly created → not owned by the directory.
            assert rows[0].directory_provider is None
    asyncio.run(run())


@pytest.mark.e2e
def test_sync_cannot_target_unconfigured_organization(create_user, login_user, whoami, monkeypatch):
    admin = create_user()
    org = whoami(login_user(admin["email"], admin["password"]))["organizations"][0]["id"]
    monkeypatch.setattr(LDAPConnectionManager, "search_users", lambda *a: [])
    monkeypatch.setattr(LDAPConnectionManager, "search_groups", lambda *a: [])
    async def run():
        from app.dependencies import async_session_maker
        async with async_session_maker() as db:
            result = await LDAPGroupSyncService(LDAPConfig(organization_id="another-org")).sync_groups(db, org)
            assert result.errors
    asyncio.run(run())


@pytest.mark.e2e
def test_sync_purges_the_results_of_members_whose_groups_changed_once_committed(
        test_client, create_user, login_user, whoami, monkeypatch, member_purges):
    """A member's groups feed the row filters compiled for them: a sync that
    changes them purges their outdated stored results -- once per sync, after
    it commits, for the members it changed only."""
    admin = create_user()
    org = whoami(login_user(admin["email"], admin["password"]))["organizations"][0]["id"]
    email = f"ldap-member-{uuid.uuid4().hex}@example.com"
    assert test_client.post(f"/api/organizations/{org}/members",
        headers={"Authorization": f"Bearer {login_user(admin['email'], admin['password'])}",
                 "X-Organization-Id": org},
        json={"organization_id": org, "email": email, "role": "member"}).status_code == 200
    member = create_user(email=email)
    member_id = whoami(login_user(email, member["password"]))["id"]
    dn = f"cn={uuid.uuid4().hex[:8]},ou=Users,dc=test,dc=com"
    # The member's verified directory binding (a sync maps only bound users);
    # seeded directly, as there is no live directory.
    provider = LDAPConnectionManager(LDAPConfig(organization_id=org)).provider_id
    identity = {"provider": provider, "guid": str(uuid.uuid4()), "sid_hex": "01", "dn": dn, "email": email}

    async def bind():
        from app.dependencies import async_session_maker
        from app.models.user import User
        async with async_session_maker() as db:
            user = await db.get(User, member_id)
            user.ldap_identity = identity
            user.ldap_subject = f"{provider}:{identity['guid']}"
            await db.commit()

    asyncio.run(bind())
    monkeypatch.setattr(LDAPConnectionManager, "read_identity", lambda self, ref: dict(identity))
    directory = {"members": [dn]}
    name = f"Sales {uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(LDAPConnectionManager, "search_users", lambda *a: [{"dn": dn, "email": email}])
    monkeypatch.setattr(LDAPConnectionManager, "search_groups", lambda *a: [
        {"dn": "cn=Sales,ou=Groups,dc=test,dc=com", "name": name, "members": list(directory["members"])}])

    async def sync():
        from app.dependencies import async_session_maker
        async with async_session_maker() as db:
            result = await LDAPGroupSyncService(LDAPConfig(organization_id=org)).sync_groups(db, org)
            assert not result.errors, result.errors

    from tests.fixtures.member_results import member_results_now, purged, seed_member_results_now, untouched

    step = seed_member_results_now(org, member_id)
    asyncio.run(sync())
    assert member_results_now(step) == purged()
    assert [c["user_ids"] for c in member_purges] == [[member_id]]
    assert member_purges[0]["organization_id"] == org
    assert len(member_purges[0]["groups"][member_id]) == 1  # committed: the new group shows

    step = seed_member_results_now(org, member_id)
    asyncio.run(sync())  # nothing changed
    assert len(member_purges) == 1
    assert member_results_now(step) == untouched()

    directory["members"] = []
    asyncio.run(sync())
    assert member_results_now(step) == purged()
    assert [c["user_ids"] for c in member_purges] == [[member_id], [member_id]]
    assert member_purges[1]["groups"][member_id] == set()


@pytest.mark.e2e
def test_a_group_removed_from_the_directory_purges_its_registered_members_only(
        test_client, create_user, login_user, whoami, monkeypatch, member_purges):
    """The removed group also holds an invited member who never registered
    (no user id): they have no stored results, and are not purged as a user."""
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org}
    email = f"ldap-member-{uuid.uuid4().hex}@example.com"
    pending_email = f"ldap-invitee-{uuid.uuid4().hex}@example.com"
    for invited in (email, pending_email):
        assert test_client.post(f"/api/organizations/{org}/members", headers=headers,
                                json={"organization_id": org, "email": invited, "role": "member"}).status_code == 200
    member = create_user(email=email)
    member_id = whoami(login_user(email, member["password"]))["id"]
    dn = f"cn={uuid.uuid4().hex[:8]},ou=Users,dc=test,dc=com"
    provider = LDAPConnectionManager(LDAPConfig(organization_id=org)).provider_id
    identity = {"provider": provider, "guid": str(uuid.uuid4()), "sid_hex": "01", "dn": dn, "email": email}

    async def bind():
        from app.dependencies import async_session_maker
        from app.models.user import User
        async with async_session_maker() as db:
            user = await db.get(User, member_id)
            user.ldap_identity = identity
            user.ldap_subject = f"{provider}:{identity['guid']}"
            await db.commit()

    asyncio.run(bind())
    monkeypatch.setattr(LDAPConnectionManager, "read_identity", lambda self, ref: dict(identity))
    monkeypatch.setattr(LDAPConnectionManager, "search_users", lambda *a: [{"dn": dn, "email": email}])
    names = {f"cn={name},ou=Groups,dc=test,dc=com": f"{name} {uuid.uuid4().hex[:6]}" for name in ("Sales", "Support")}
    sales, support = names
    directory = {"groups": [sales]}
    monkeypatch.setattr(LDAPConnectionManager, "search_groups", lambda *a: [
        {"dn": g, "name": names[g], "members": [dn]} for g in directory["groups"]])

    async def sync():
        from app.dependencies import async_session_maker
        async with async_session_maker() as db:
            result = await LDAPGroupSyncService(LDAPConfig(organization_id=org)).sync_groups(db, org)
            assert not result.errors, result.errors

    async def add_pending_member():
        """The invite joins the directory group (no API adds a pending
        member to a directory-owned group)."""
        from app.dependencies import async_session_maker
        from app.models.group import Group
        from app.models.group_membership import GroupMembership
        async with async_session_maker() as db:
            group_id = (await db.execute(select(Group.id).where(
                Group.organization_id == org, Group.external_id == sales))).scalar_one()
            pending_id = (await db.execute(select(Membership.id).where(
                Membership.organization_id == org, Membership.email == pending_email))).scalar_one()
            db.add(GroupMembership(group_id=group_id, membership_id=pending_id))
            await db.commit()

    from tests.fixtures.member_results import member_results_now, purged, seed_member_results_now

    asyncio.run(sync())
    asyncio.run(add_pending_member())
    directory["groups"] = [support]
    step = seed_member_results_now(org, member_id)
    asyncio.run(sync())
    assert member_results_now(step) == purged()
    assert member_purges[-1]["user_ids"] == [member_id]


@pytest.mark.e2e
def test_a_sync_that_changes_a_members_groups_purges_their_outdated_results(
        test_client, create_user, login_user, whoami, monkeypatch):
    """The real purge, end to end: the member the sync adds to a group keeps
    only their latest result, cleared; the admin it did not change keeps all."""
    from tests.fixtures.member_results import member_results_now, purged, seed_member_results_now, untouched

    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    me = whoami(token)
    org = me["organizations"][0]["id"]
    email = f"ldap-member-{uuid.uuid4().hex}@example.com"
    assert test_client.post(f"/api/organizations/{org}/members",
                            headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org},
                            json={"organization_id": org, "email": email, "role": "member"}).status_code == 200
    member = create_user(email=email)
    member_id = whoami(login_user(email, member["password"]))["id"]
    dn = f"cn={uuid.uuid4().hex[:8]},ou=Users,dc=test,dc=com"
    provider = LDAPConnectionManager(LDAPConfig(organization_id=org)).provider_id
    identity = {"provider": provider, "guid": str(uuid.uuid4()), "sid_hex": "01", "dn": dn, "email": email}

    async def bind():
        from app.dependencies import async_session_maker
        from app.models.user import User
        async with async_session_maker() as db:
            user = await db.get(User, member_id)
            user.ldap_identity = identity
            user.ldap_subject = f"{provider}:{identity['guid']}"
            await db.commit()

    asyncio.run(bind())
    monkeypatch.setattr(LDAPConnectionManager, "read_identity", lambda self, ref: dict(identity))
    monkeypatch.setattr(LDAPConnectionManager, "search_users", lambda *a: [{"dn": dn, "email": email}])
    monkeypatch.setattr(LDAPConnectionManager, "search_groups", lambda *a: [
        {"dn": "cn=Sales,ou=Groups,dc=test,dc=com", "name": f"Sales {uuid.uuid4().hex[:6]}", "members": [dn]}])
    changed, unchanged = seed_member_results_now(org, member_id), seed_member_results_now(org, me["id"])

    async def sync():
        from app.dependencies import async_session_maker
        async with async_session_maker() as db:
            result = await LDAPGroupSyncService(LDAPConfig(organization_id=org)).sync_groups(db, org)
            assert not result.errors, result.errors

    asyncio.run(sync())
    assert member_results_now(changed) == purged()
    assert member_results_now(unchanged) == untouched()
