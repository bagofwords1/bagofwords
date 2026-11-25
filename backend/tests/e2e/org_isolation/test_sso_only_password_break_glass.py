"""Who may still sign in with a password when the deployment is ``sso_only``.

Contract:
* Single organization: its admin is the instance owner and keeps the password
  break-glass; members do not.
* Several organizations on one instance: administering one of them is not
  enough. Only an instance superuser keeps the break-glass.
"""
import asyncio

import pytest
from sqlalchemy import update

from tests.e2e.org_isolation.identity_helpers import (
    auth_mode, bootstrap_admin, feature, invite, login, register, second_org_admin, unique_email,
)


def _member(client, admin):
    email = unique_email("member")
    _, token = invite(client, admin["token"], admin["org_id"], email)
    assert register(client, email, invite_token=token).status_code == 201
    return email


def _make_superuser(email):
    # Superusers are designated by the operator in the database; no API grants it.
    from app.dependencies import async_session_maker
    from app.models.user import User

    async def _set():
        async with async_session_maker() as db:
            await db.execute(update(User).where(User.email == email).values(is_superuser=True))
            await db.commit()

    asyncio.run(_set())


def test_single_org_admin_keeps_password_login_and_members_do_not(test_client):
    admin = bootstrap_admin(test_client)
    member = _member(test_client, admin)
    with auth_mode("sso_only"):
        assert login(test_client, admin["email"]).status_code == 200
        assert login(test_client, member).status_code == 400
        assert login(test_client, admin["email"], "wrong-password").status_code == 400


def test_multi_org_restricts_password_login_to_superusers(test_client):
    with feature("allow_multiple_organizations", True):
        first = bootstrap_admin(test_client)
        second = second_org_admin(test_client, first)
        member = _member(test_client, first)
        operator = _member(test_client, first)
        _make_superuser(operator)

        with auth_mode("sso_only"):
            for org_admin in (first, second):
                assert login(test_client, org_admin["email"]).status_code == 400
            assert login(test_client, member).status_code == 400
            assert login(test_client, operator).status_code == 200
            assert login(test_client, operator, "wrong-password").status_code == 400

        # Outside sso_only nothing changes for password users.
        assert login(test_client, second["email"]).status_code == 200
