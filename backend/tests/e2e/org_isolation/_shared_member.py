"""A person who belongs to two organizations, built through the real API.

Not a conftest on purpose (see ``_cast``): modules import what they need.
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker

from ._cast import h, two_orgs  # noqa: F401


def run(coro):
    """Run a direct-database step (states the API cannot produce)."""
    return asyncio.run(coro)


def invite(test_client, *, admin_token, org_id, email, role="member"):
    resp = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email, "role": role},
        headers=h(admin_token, org_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def accept_invite(test_client, *, admin_token, org_id, membership_id, user_token):
    """The invited person follows the invite link while signed in."""
    link = test_client.get(
        f"/api/organizations/{org_id}/members/{membership_id}/invite-link",
        headers=h(admin_token, org_id),
    )
    assert link.status_code == 200, link.text
    accepted = test_client.post(
        "/api/invites/accept", json={"token": link.json()["token"]},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["organization_id"] == org_id


def org_ids(whoami, token):
    return {o["id"] for o in whoami(token)["organizations"]}


def is_member(test_client, token, org_id):
    """Whether ``token`` is let into ``org_id`` (any member may read their inbox count)."""
    return test_client.get("/api/notifications/count", headers=h(token, org_id)).status_code == 200


@pytest.fixture
def shared_member(test_client, two_orgs, whoami):
    """``two_orgs`` plus one person who is a plain member of both A and B.

    They registered through A's invite, then B's admin added their existing
    account (which joins directly).
    """
    o = two_orgs
    me = whoami(o["a_member_token"])
    added = invite(test_client, admin_token=o["b_token"], org_id=o["b_org"], email=me["email"])
    assert added["user_id"] == me["id"]
    assert org_ids(whoami, o["a_member_token"]) == {o["a_org"], o["b_org"]}
    return {**o, "shared_token": o["a_member_token"], "shared_id": me["id"], "shared_email": me["email"]}


async def set_user_active(user_id, active):
    from app.models.user import User

    async with async_session_maker() as db:
        user = await db.get(User, str(user_id))
        user.is_active = active
        await db.commit()


async def get_user_row(user_id):
    from app.models.user import User

    async with async_session_maker() as db:
        user = await db.get(User, str(user_id))
        return {"email": user.email, "name": user.name, "is_active": user.is_active}


def new_email(prefix="person"):
    return f"{prefix}_{uuid.uuid4().hex[:10]}@test.com"


async def set_superuser(user_id, value=True):
    # Direct write: the instance-administrator flag is set by the operator
    # (configuration or by hand), never through the API.
    from app.models.user import User

    async with async_session_maker() as db:
        user = await db.get(User, str(user_id))
        user.is_superuser = value
        await db.commit()


async def accounts_with_email(email):
    from sqlalchemy import func, select

    from app.models.user import User

    async with async_session_maker() as db:
        return (await db.execute(
            select(func.count(User.id)).where(func.lower(User.email) == email.lower())
        )).scalar()


@pytest.fixture(params=["flag", "configured_list"])
def instance_admin(request, monkeypatch, two_orgs, whoami):
    """``two_orgs`` where A's plain member is an instance administrator,
    either by the stored flag or by being named in the configured list."""
    from app.settings.config import settings as bow_settings

    o = two_orgs
    email = whoami(o["a_member_token"])["email"]
    if request.param == "flag":
        run(set_superuser(o["a_member_id"]))
    else:
        monkeypatch.setattr(bow_settings.bow_config.auth, "superuser_emails", [email.upper()])
    return {**o, "admin_email": email, "admin_id": o["a_member_id"], "admin_token": o["a_member_token"]}


@pytest.fixture(params=[True, False], ids=["multi_org_on", "multi_org_off"])
def multi_org_flag(request, monkeypatch, two_orgs):
    """Run with multiple organizations enabled and disabled (set after the
    two organizations exist)."""
    from app.settings.config import settings as bow_settings

    monkeypatch.setattr(bow_settings.bow_config.features, "allow_multiple_organizations", request.param)
    return request.param
