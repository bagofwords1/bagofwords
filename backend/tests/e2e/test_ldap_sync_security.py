"""A complete directory snapshot may remove only directory-owned access."""
import asyncio
import uuid
import pytest
from sqlalchemy import select
from app.ee.ldap.connection import LDAPConnectionManager
from app.ee.ldap.sync_service import LDAPGroupSyncService
from app.models.membership import Membership
from app.settings.bow_config import LDAPConfig


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
