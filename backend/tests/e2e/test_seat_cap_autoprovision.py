"""e2e tests: the license seat cap (max_users) is enforced on the AUTO-PROVISIONING
paths, not just admin invites.

Regression coverage for the bug where an EE org with a user cap kept gaining
uninvited members through Entra/SSO domain signup, chat auto-provision, LDAP sync,
SCIM provisioning, and OIDC group sync — while the admin's manual invite (the only
guarded path) was blocked at the now-overflowed count.

Semantics under test ("block only truly new members"): existing members are never
removed or blocked; only the creation of a *new* membership beyond the cap is
refused. Each path degrades in its own idiomatic way:
  - chat auto-provision / OIDC sync → skip (return None / no-op)
  - LDAP sync                       → fill up to the cap, skip the rest
  - SCIM provisioning               → reject with HTTP 402

The cap is driven by seeding the in-process license cache directly (same approach
as test_license_limits.py) — no signing key needed.
"""
import contextlib
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select, func

import app.ee.license as license_mod
from app.ee.license import LicenseInfo


@contextlib.contextmanager
def _license(*, max_users: int = -1, max_agents: int = -1):
    prev_cache = license_mod._cached_license
    prev_init = license_mod._cache_initialized
    license_mod._cached_license = LicenseInfo(
        licensed=True,
        tier="enterprise",
        org_name="Test Org",
        expires_at=datetime.now(timezone.utc) + timedelta(days=365),
        max_users=max_users,
        max_agents=max_agents,
    )
    license_mod._cache_initialized = True
    try:
        yield
    finally:
        license_mod._cached_license = prev_cache
        license_mod._cache_initialized = prev_init


def _admin_setup(create_user, login_user, whoami):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return org_id


async def _count(db, org_id) -> int:
    from app.models.membership import Membership
    return (await db.execute(
        select(func.count(Membership.id)).where(
            Membership.organization_id == org_id,
            Membership.deleted_at.is_(None),
        )
    )).scalar() or 0


async def _fill_to(db, org_id, target):
    """Top the org up to `target` memberships with pending-invite rows."""
    from app.models.membership import Membership
    current = await _count(db, org_id)
    for _ in range(target - current):
        db.add(Membership(
            email=f"seed_{uuid.uuid4().hex[:8]}@seed.com",
            organization_id=org_id,
            role="member",
        ))
    await db.commit()


async def _make_user(db, email=None):
    from app.models.user import User
    from fastapi_users.password import PasswordHelper
    ph = PasswordHelper()
    email = email or f"u_{uuid.uuid4().hex[:8]}@ext.com"
    user = User(
        email=email,
        name=email.split("@")[0],
        hashed_password=ph.hash(ph.generate()),
        is_active=True,
        is_verified=True,
        is_superuser=False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _set_signup_policy(db, org_id, domains):
    """Enable the org's domain-signup policy for the given domains."""
    from app.models.organization_settings import OrganizationSettings
    s = (await db.execute(
        select(OrganizationSettings).where(OrganizationSettings.organization_id == org_id)
    )).scalar_one_or_none()
    assert s is not None, "org should have a settings row"
    config = dict(s.config or {})
    config["signup_policy"] = {
        "enabled": True,
        "allowed_domains": domains,
        "auto_invite_role": "member",
    }
    s.config = config
    # SQLAlchemy JSON mutation tracking: reassign so the change is flushed.
    await db.commit()


# ---------------------------------------------------------------------------
# shared helper
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_seats_helper_counts_caps_and_enforces(create_user, login_user, whoami):
    from app.dependencies import async_session_maker
    from app.core.seats import (
        count_org_memberships, seats_remaining, has_seat_for, enforce_seat_limit,
    )
    from fastapi import HTTPException

    org_id = _admin_setup(create_user, login_user, whoami)  # 1 membership (admin)

    async with async_session_maker() as db:
        # Unlimited when no cap: remaining is None, always has a seat.
        assert await seats_remaining(db, org_id) is None
        assert await has_seat_for(db, org_id, adding=1000) is True

        with _license(max_users=3):
            assert await count_org_memberships(db, org_id) == 1
            assert await seats_remaining(db, org_id) == 2
            assert await has_seat_for(db, org_id, adding=2) is True
            assert await has_seat_for(db, org_id, adding=3) is False
            # enforce raises 402 once over
            await enforce_seat_limit(db, org_id, adding=2)  # ok
            with pytest.raises(HTTPException) as ei:
                await enforce_seat_limit(db, org_id, adding=3)
            assert ei.value.status_code == 402


# ---------------------------------------------------------------------------
# chat auto-provision (domain-admitted path)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_auto_provision_blocked_when_full(create_user, login_user, whoami):
    from app.dependencies import async_session_maker
    from app.core.auth import auto_provision_user_for_org

    org_id = _admin_setup(create_user, login_user, whoami)

    async with async_session_maker() as db:
        await _set_signup_policy(db, org_id, ["ext.com"])
        await _fill_to(db, org_id, 2)  # org now full at cap 2

    with _license(max_users=2):
        async with async_session_maker() as db:
            user = await auto_provision_user_for_org(
                db, org_id, f"new_{uuid.uuid4().hex[:6]}@ext.com"
            )
        assert user is None  # refused — org is at the seat cap

    async with async_session_maker() as db:
        assert await _count(db, org_id) == 2  # no new membership minted


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_auto_provision_allowed_when_seat_free(create_user, login_user, whoami):
    from app.dependencies import async_session_maker
    from app.core.auth import auto_provision_user_for_org

    org_id = _admin_setup(create_user, login_user, whoami)  # 1 membership

    async with async_session_maker() as db:
        await _set_signup_policy(db, org_id, ["ext.com"])

    with _license(max_users=5):  # room to spare
        async with async_session_maker() as db:
            user = await auto_provision_user_for_org(
                db, org_id, f"new_{uuid.uuid4().hex[:6]}@ext.com"
            )
        assert user is not None

    async with async_session_maker() as db:
        assert await _count(db, org_id) == 2


# ---------------------------------------------------------------------------
# OIDC group sync
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_oidc_ensure_membership_blocked_when_full(create_user, login_user, whoami):
    from app.dependencies import async_session_maker
    from app.ee.oidc.group_sync_service import _ensure_org_membership

    org_id = _admin_setup(create_user, login_user, whoami)

    async with async_session_maker() as db:
        await _fill_to(db, org_id, 2)  # full at cap 2
        outsider = await _make_user(db)

    with _license(max_users=2):
        async with async_session_maker() as db:
            await _ensure_org_membership(db, org_id, str(outsider.id))
            await db.commit()

    async with async_session_maker() as db:
        assert await _count(db, org_id) == 2  # outsider not added


# ---------------------------------------------------------------------------
# LDAP sync — fills up to the cap, skips the rest
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_ldap_ensure_memberships_fills_up_to_cap(create_user, login_user, whoami):
    from app.dependencies import async_session_maker
    from app.ee.ldap.sync_service import LDAPGroupSyncService

    org_id = _admin_setup(create_user, login_user, whoami)  # 1 membership, cap 3 → 2 seats

    async with async_session_maker() as db:
        users = [await _make_user(db) for _ in range(4)]
        user_ids = {str(u.id) for u in users}

    with _license(max_users=3):
        async with async_session_maker() as db:
            # _ensure_org_memberships doesn't touch self; skip the LDAPConfig-heavy
            # __init__ and call it on a bare instance.
            svc = object.__new__(LDAPGroupSyncService)
            await svc._ensure_org_memberships(db, org_id, user_ids)
            await db.commit()

    async with async_session_maker() as db:
        # admin (1) + exactly the 2 remaining seats filled = 3, not 5
        assert await _count(db, org_id) == 3


# ---------------------------------------------------------------------------
# SCIM provisioning — rejects with 402
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_scim_create_user_blocked_when_full(create_user, login_user, whoami):
    from app.dependencies import async_session_maker
    from app.ee.scim.service import ScimUserService
    from app.ee.scim.schemas import ScimUserCreate, ScimEmail
    from fastapi import HTTPException

    org_id = _admin_setup(create_user, login_user, whoami)

    async with async_session_maker() as db:
        await _fill_to(db, org_id, 2)  # full at cap 2

    email = f"scim_{uuid.uuid4().hex[:6]}@ext.com"
    payload = ScimUserCreate(
        userName=email,
        emails=[ScimEmail(value=email, primary=True)],
        active=True,
    )

    with _license(max_users=2):
        async with async_session_maker() as db:
            svc = ScimUserService()
            with pytest.raises(HTTPException) as ei:
                await svc.create_user(db, org_id, payload)
            assert ei.value.status_code == 402

    async with async_session_maker() as db:
        assert await _count(db, org_id) == 2  # nothing provisioned
