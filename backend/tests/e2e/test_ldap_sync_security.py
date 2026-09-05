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
