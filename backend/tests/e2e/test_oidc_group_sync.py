"""
E2E tests for OIDC group sync.

Tests the full flow: login with groups claim → Group + GroupMembership created → RBAC applied.
Uses mocked token responses — no real Entra/Google accounts needed.
"""
import pytest
import jwt as pyjwt
import uuid
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone, timedelta
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend


# ── Test key generation (same pattern as test_ldap.py) ──

def _generate_test_keys():
    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_pem, public_pem


TEST_PRIVATE_KEY, TEST_PUBLIC_KEY = _generate_test_keys()


def _create_test_license(tier="enterprise"):
    now = datetime.now(timezone.utc)
    payload = {
        "iss": "bagofwords.com",
        "sub": "lic_test_oidc",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=365)).timestamp()),
        "tier": tier,
        "org_name": "OIDC Test Corp",
        "features": [],
    }
    token = pyjwt.encode(payload, TEST_PRIVATE_KEY, algorithm="RS256")
    return f"bow_lic_{token}"


# ── Fixtures ──

@pytest.fixture
def license_env_cleanup():
    import os
    from app.ee.license import clear_license_cache

    original = os.environ.get("BOW_LICENSE_KEY")
    yield
    if original:
        os.environ["BOW_LICENSE_KEY"] = original
    elif "BOW_LICENSE_KEY" in os.environ:
        del os.environ["BOW_LICENSE_KEY"]
    clear_license_cache()


@pytest.fixture
def patch_license_key(license_env_cleanup):
    import app.ee.license as license_module

    original_key = license_module.LICENSE_PUBLIC_KEY
    license_module.LICENSE_PUBLIC_KEY = TEST_PUBLIC_KEY
    yield
    license_module.LICENSE_PUBLIC_KEY = original_key


@pytest.fixture
def enterprise_license(patch_license_key):
    from app.ee.license import clear_license_cache
    from app.settings.config import settings
    from app.settings.bow_config import LicenseConfig

    test_license = _create_test_license(tier="enterprise")
    if not hasattr(settings.bow_config, "license") or not settings.bow_config.license:
        settings.bow_config.license = LicenseConfig(key=test_license)
    else:
        settings.bow_config.license.key = test_license
    clear_license_cache()
    yield


@pytest.fixture
def oidc_setup(test_client, create_user, login_user, whoami, enterprise_license):
    """Set up a user, org, and auth headers."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    user_info = whoami(token)
    org_id = user_info["organizations"][0]["id"]
    user_id = user_info["id"]
    return {
        "user_token": token,
        "user_id": user_id,
        "org_id": org_id,
        "email": user["email"],
        "headers": {
            "Authorization": f"Bearer {token}",
            "X-Organization-Id": org_id,
        },
    }


def _make_id_token(groups=None, extra_claims=None):
    """Create a minimal unsigned JWT with group claims for testing."""
    claims = {
        "sub": "oidc-user-123",
        "email": "oidc-user@test.com",
        "iss": "https://login.microsoftonline.com/test-tenant/v2.0",
        "aud": "test-client-id",
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    }
    if groups is not None:
        claims["groups"] = groups
    if extra_claims:
        claims.update(extra_claims)
    return pyjwt.encode(claims, "secret", algorithm="HS256")


# ============================================================================
# OIDC Group Sync Service Tests (direct DB tests)
# ============================================================================


@pytest.mark.e2e
class TestOidcGroupSyncService:

    @pytest.mark.asyncio
    async def test_sync_creates_groups_and_memberships(self, oidc_setup):
        """When sync is called with group IDs, Group + GroupMembership rows are created."""
        from app.dependencies import async_session_maker
        from app.ee.oidc.group_sync_service import sync_user_oidc_groups
        from app.models.group import Group
        from app.models.group_membership import GroupMembership
        from sqlalchemy import select

        group_ids = ["entra-group-aaa", "entra-group-bbb"]
        group_names = {
            "entra-group-aaa": "AllFabric",
            "entra-group-bbb": "MinimalFabric",
        }

        async with async_session_maker() as db:
            result = await sync_user_oidc_groups(
                db=db,
                user_id=oidc_setup["user_id"],
                organization_id=oidc_setup["org_id"],
                group_ids=group_ids,
                group_names=group_names,
            )

        assert result.groups_created == 2
        assert result.memberships_added == 2

        # Verify groups in DB
        async with async_session_maker() as db:
            groups = (
                await db.execute(
                    select(Group).where(
                        Group.organization_id == oidc_setup["org_id"],
                        Group.external_provider == "oidc",
                    )
                )
            ).scalars().all()
            assert len(groups) == 2
            names = {g.name for g in groups}
            assert names == {"AllFabric", "MinimalFabric"}
            assert all(g.external_provider == "oidc" for g in groups)

            # Verify memberships
            for g in groups:
                memberships = (
                    await db.execute(
                        select(GroupMembership).where(
                            GroupMembership.group_id == g.id,
                            GroupMembership.user_id == oidc_setup["user_id"],
                        )
                    )
                ).scalars().all()
                assert len(memberships) == 1

    @pytest.mark.asyncio
    async def test_sync_updates_memberships_on_group_change(self, oidc_setup):
        """When user's groups change between syncs, memberships are updated."""
        from app.dependencies import async_session_maker
        from app.ee.oidc.group_sync_service import sync_user_oidc_groups
        from app.models.group_membership import GroupMembership
        from app.models.group import Group
        from sqlalchemy import select

        # First sync: groups A and B
        async with async_session_maker() as db:
            await sync_user_oidc_groups(
                db=db,
                user_id=oidc_setup["user_id"],
                organization_id=oidc_setup["org_id"],
                group_ids=["group-aaa", "group-bbb"],
                group_names={"group-aaa": "GroupA", "group-bbb": "GroupB"},
            )

        # Second sync: groups B and C (removed from A, added to C)
        async with async_session_maker() as db:
            result = await sync_user_oidc_groups(
                db=db,
                user_id=oidc_setup["user_id"],
                organization_id=oidc_setup["org_id"],
                group_ids=["group-bbb", "group-ccc"],
                group_names={"group-bbb": "GroupB", "group-ccc": "GroupC"},
            )

        assert result.groups_created == 1  # GroupC
        assert result.memberships_added == 1  # added to GroupC
        assert result.memberships_removed == 1  # removed from GroupA

        # Verify final state
        async with async_session_maker() as db:
            stmt = (
                select(Group.external_id)
                .join(GroupMembership, GroupMembership.group_id == Group.id)
                .where(
                    Group.external_provider == "oidc",
                    GroupMembership.user_id == oidc_setup["user_id"],
                )
            )
            member_groups = set((await db.execute(stmt)).scalars().all())
            assert member_groups == {"group-bbb", "group-ccc"}

    @pytest.mark.asyncio
    async def test_sync_removes_stale_memberships(self, oidc_setup):
        """User removed from a group → GroupMembership deleted on next sync."""
        from app.dependencies import async_session_maker
        from app.ee.oidc.group_sync_service import sync_user_oidc_groups

        # First sync: in groups A and B
        async with async_session_maker() as db:
            await sync_user_oidc_groups(
                db=db,
                user_id=oidc_setup["user_id"],
                organization_id=oidc_setup["org_id"],
                group_ids=["group-aaa", "group-bbb"],
            )

        # Second sync: only in group A (removed from B)
        async with async_session_maker() as db:
            result = await sync_user_oidc_groups(
                db=db,
                user_id=oidc_setup["user_id"],
                organization_id=oidc_setup["org_id"],
                group_ids=["group-aaa"],
            )

        assert result.memberships_removed == 1

    @pytest.mark.asyncio
    async def test_groups_tagged_with_oidc_provider(self, oidc_setup):
        """OIDC-synced groups have external_provider='oidc'."""
        from app.dependencies import async_session_maker
        from app.ee.oidc.group_sync_service import sync_user_oidc_groups
        from app.models.group import Group
        from sqlalchemy import select

        async with async_session_maker() as db:
            await sync_user_oidc_groups(
                db=db,
                user_id=oidc_setup["user_id"],
                organization_id=oidc_setup["org_id"],
                group_ids=["ext-group-1"],
                group_names={"ext-group-1": "TestGroup"},
            )

        async with async_session_maker() as db:
            group = (
                await db.execute(
                    select(Group).where(Group.external_id == "ext-group-1")
                )
            ).scalar_one()
            assert group.external_provider == "oidc"
            assert group.external_id == "ext-group-1"

    @pytest.mark.asyncio
    async def test_oidc_sync_does_not_touch_ldap_groups(self, oidc_setup):
        """OIDC sync only manages groups with external_provider='oidc'."""
        from app.dependencies import async_session_maker
        from app.ee.oidc.group_sync_service import sync_user_oidc_groups
        from app.models.group import Group
        from app.models.group_membership import GroupMembership
        from sqlalchemy import select

        # Pre-create an LDAP group with a membership
        async with async_session_maker() as db:
            ldap_group = Group(
                organization_id=oidc_setup["org_id"],
                name="LDAPGroup",
                external_id="cn=ldapgroup,dc=test",
                external_provider="ldap",
            )
            db.add(ldap_group)
            await db.flush()
            db.add(GroupMembership(
                group_id=ldap_group.id,
                user_id=oidc_setup["user_id"],
            ))
            await db.commit()

        # Run OIDC sync with different groups
        async with async_session_maker() as db:
            await sync_user_oidc_groups(
                db=db,
                user_id=oidc_setup["user_id"],
                organization_id=oidc_setup["org_id"],
                group_ids=["oidc-group-1"],
            )

        # Verify LDAP group and membership untouched
        async with async_session_maker() as db:
            ldap_memberships = (
                await db.execute(
                    select(GroupMembership)
                    .join(Group, Group.id == GroupMembership.group_id)
                    .where(
                        Group.external_provider == "ldap",
                        GroupMembership.user_id == oidc_setup["user_id"],
                    )
                )
            ).scalars().all()
            assert len(ldap_memberships) == 1

    @pytest.mark.asyncio
    async def test_multiple_users_same_group(self, oidc_setup, test_client, create_user, login_user, whoami):
        """Two users in same OIDC group → both get membership, Group created once."""
        from app.dependencies import async_session_maker
        from app.ee.oidc.group_sync_service import sync_user_oidc_groups
        from app.models.group import Group
        from app.models.group_membership import GroupMembership
        from app.settings.config import settings
        from sqlalchemy import select

        # Enable uninvited signups for this test
        original = settings.bow_config.features.allow_uninvited_signups
        settings.bow_config.features.allow_uninvited_signups = True

        # Create second user
        email2 = f"user2_{uuid.uuid4().hex[:6]}@test.com"
        create_user(email=email2, password="TestPass123!")
        token2 = login_user(email2, "TestPass123!")
        user2_info = whoami(token2)
        user2_id = user2_info["id"]

        group_names = {"shared-group": "SharedGroup"}

        # User 1 syncs
        async with async_session_maker() as db:
            r1 = await sync_user_oidc_groups(
                db=db,
                user_id=oidc_setup["user_id"],
                organization_id=oidc_setup["org_id"],
                group_ids=["shared-group"],
                group_names=group_names,
            )
        assert r1.groups_created == 1

        # User 2 syncs same group
        async with async_session_maker() as db:
            r2 = await sync_user_oidc_groups(
                db=db,
                user_id=user2_id,
                organization_id=oidc_setup["org_id"],
                group_ids=["shared-group"],
                group_names=group_names,
            )
        assert r2.groups_created == 0  # Group already exists
        assert r2.memberships_added == 1

        # Verify: 1 group, 2 memberships
        async with async_session_maker() as db:
            groups = (
                await db.execute(
                    select(Group).where(Group.external_id == "shared-group")
                )
            ).scalars().all()
            assert len(groups) == 1

            memberships = (
                await db.execute(
                    select(GroupMembership).where(
                        GroupMembership.group_id == groups[0].id
                    )
                )
            ).scalars().all()
            assert len(memberships) == 2

        # Restore setting
        settings.bow_config.features.allow_uninvited_signups = original

    @pytest.mark.asyncio
    async def test_group_name_updated_on_sync(self, oidc_setup):
        """If group is renamed externally, Group.name updates on next sync."""
        from app.dependencies import async_session_maker
        from app.ee.oidc.group_sync_service import sync_user_oidc_groups
        from app.models.group import Group
        from sqlalchemy import select

        # First sync with original name
        async with async_session_maker() as db:
            await sync_user_oidc_groups(
                db=db,
                user_id=oidc_setup["user_id"],
                organization_id=oidc_setup["org_id"],
                group_ids=["rename-group"],
                group_names={"rename-group": "OldName"},
            )

        # Second sync with new name
        async with async_session_maker() as db:
            result = await sync_user_oidc_groups(
                db=db,
                user_id=oidc_setup["user_id"],
                organization_id=oidc_setup["org_id"],
                group_ids=["rename-group"],
                group_names={"rename-group": "NewName"},
            )

        assert result.groups_updated == 1

        async with async_session_maker() as db:
            group = (
                await db.execute(
                    select(Group).where(Group.external_id == "rename-group")
                )
            ).scalar_one()
            assert group.name == "NewName"

    @pytest.mark.asyncio
    async def test_empty_group_ids_is_noop(self, oidc_setup):
        """Passing empty group_ids does nothing."""
        from app.dependencies import async_session_maker
        from app.ee.oidc.group_sync_service import sync_user_oidc_groups

        async with async_session_maker() as db:
            result = await sync_user_oidc_groups(
                db=db,
                user_id=oidc_setup["user_id"],
                organization_id=oidc_setup["org_id"],
                group_ids=[],
            )

        assert result.groups_created == 0
        assert result.memberships_added == 0
