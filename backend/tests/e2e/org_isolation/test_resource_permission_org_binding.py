"""Resource permission checks are bound to the caller's organization.

Contract:
* a role or org-level permission (full admin included) never authorizes a
  resource id that belongs to another organization — the check answers as for
  a missing resource, before any allow tier;
* a principal who is not bound to the organization gets an audited 403 from
  the resource-permission decorator, not a server error;
* `model=` object scoping covers the integrations routes' `platform_id`.

`check_resource_permissions` and `requires_resource_permission` are the public
permission API routes build on; they are exercised directly with a real session
because HTTP routes add their own service-level scoping on top.
"""
import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.permissions_decorator import check_resource_permissions, requires_resource_permission
from app.dependencies import async_session_maker
from app.ee.audit.models import AuditLog
from app.models.organization import Organization
from app.models.user import User

from .agent_surface_fixtures import CHINOOK_PATH, allow_multiple_orgs, h, sqlite_agent, two_orgs  # noqa: F401


async def _check(user_id, org_id, resource_type, ids, permission):
    async with async_session_maker() as db:
        await check_resource_permissions(db, str(user_id), str(org_id), resource_type, ids, permission)


@pytest.mark.parametrize("permission", ["create_entities", "manage_instructions", "view"])
def test_admin_permission_does_not_cover_another_organizations_data_source(two_orgs, sqlite_agent, permission):
    o = two_orgs
    own = sqlite_agent(token=o["home_token"], org_id=o["home_org"])
    foreign = sqlite_agent(token=o["other_token"], org_id=o["other_org"], is_public=True)

    for ids in ([foreign["id"]], [own["id"], foreign["id"]]):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_check(o["home_user_id"], o["home_org"], "data_source", ids, permission))
        assert exc.value.status_code in (403, 404)
        assert foreign["name"] not in str(exc.value.detail)

    # Own resources still pass for the admin.
    asyncio.run(_check(o["home_user_id"], o["home_org"], "data_source", [own["id"]], permission))


def test_admin_permission_does_not_cover_another_organizations_connection(two_orgs, create_connection):
    o = two_orgs

    def _conn(token, org_id):
        return create_connection(name=f"c_{uuid.uuid4().hex[:6]}", type="sqlite",
                                 config={"database": str(CHINOOK_PATH)}, credentials={},
                                 user_token=token, org_id=org_id)

    own, foreign = _conn(o["home_token"], o["home_org"]), _conn(o["other_token"], o["other_org"])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_check(o["home_user_id"], o["home_org"], "connection", [foreign["id"]], "manage_connection"))
    assert exc.value.status_code in (403, 404)
    asyncio.run(_check(o["home_user_id"], o["home_org"], "connection", [own["id"]], "manage_connection"))


@requires_resource_permission("data_source", "view")
async def _guarded(data_source_id: str, current_user=None, organization=None, db=None):
    return "reached"


async def _call_guarded(user_id, org_id, data_source_id):
    async with async_session_maker() as db:
        user = await db.get(User, user_id)
        org = await db.get(Organization, org_id)
        try:
            return await _guarded(data_source_id=data_source_id, current_user=user, organization=org, db=db)
        except HTTPException as e:
            return e.status_code


async def _denials(org_id, user_id):
    async with async_session_maker() as db:
        rows = (await db.execute(select(AuditLog).where(
            AuditLog.action == "access.denied",
            AuditLog.organization_id == str(org_id),
            AuditLog.user_id == str(user_id),
        ))).scalars().all()
        return [(r.resource_type, r.resource_id, (r.details or {}).get("reason")) for r in rows]


def test_resource_decorator_denies_and_audits_a_non_member(two_orgs, sqlite_agent):
    o = two_orgs
    ds = sqlite_agent(token=o["other_token"], org_id=o["other_org"], is_public=True)

    # home admin is not bound to `other`.
    assert asyncio.run(_call_guarded(o["home_user_id"], o["other_org"], ds["id"])) == 403
    assert ("data_source", ds["id"], "not_a_member") in asyncio.run(_denials(o["other_org"], o["home_user_id"]))

    # The organization's own admin reaches the handler.
    assert asyncio.run(_call_guarded(o["other_user_id"], o["other_org"], ds["id"])) == "reached"


def test_resource_decorator_treats_another_organizations_id_as_missing(two_orgs, sqlite_agent):
    o = two_orgs
    own = sqlite_agent(token=o["home_token"], org_id=o["home_org"])
    foreign = sqlite_agent(token=o["other_token"], org_id=o["other_org"], is_public=True)

    # Full admin of home, acting in home, naming the other organization's agent.
    assert asyncio.run(_call_guarded(o["home_user_id"], o["home_org"], foreign["id"])) == 404
    assert asyncio.run(_call_guarded(o["home_user_id"], o["home_org"], own["id"])) == "reached"
