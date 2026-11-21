"""Leaving an organization ends what the person held there.

Contract:

* Removing a member also removes their roles and resource grants in that
  organization and revokes the API keys they created there, so a later
  re-invite grants exactly the invited role.
* Nothing they hold in another organization is touched.
* A membership retired by a directory sync (soft-deleted) is not a membership:
  the organization leaves the person's organization list and its seat count,
  and what they held there is unusable. Unlike removal it is reversible: if
  the directory restores them, their roles and keys work again.
"""
import uuid

from app.dependencies import async_session_maker

from ._shared_member import (  # noqa: F401
    accept_invite, h, invite, is_member, run, shared_member, two_orgs,
)


def _members(test_client, token, org_id):
    return test_client.get(f"/api/organizations/{org_id}/members", headers=h(token, org_id)).json()


def _row(test_client, token, org_id, user_id):
    return next(m for m in _members(test_client, token, org_id) if m["user_id"] == user_id)


def _remove(test_client, token, org_id, membership_id):
    resp = test_client.delete(
        f"/api/organizations/{org_id}/members/{membership_id}", headers=h(token, org_id),
    )
    assert resp.status_code == 204, resp.text


def _grants(test_client, token, org_id, user_id):
    resp = test_client.get(
        f"/api/organizations/{org_id}/resource-grants",
        params={"principal_type": "user", "principal_id": user_id}, headers=h(token, org_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_removed_member_comes_back_with_only_the_invited_role(
    test_client, two_orgs, whoami, create_api_key, create_data_source, dynamic_sqlite_db,
):
    o = two_orgs
    admin, org, member, member_id = o["a_token"], o["a_org"], o["a_member_token"], o["a_member_id"]
    email = whoami(member)["email"]
    row = _row(test_client, admin, org, member_id)

    # While a member they are promoted, granted a data source, and mint a key.
    promoted = test_client.put(
        f"/api/organizations/{org}/members/{row['id']}", json={"role": "admin"}, headers=h(admin, org),
    )
    assert promoted.status_code == 200, promoted.text
    ds = create_data_source(
        name=f"ds_{uuid.uuid4().hex[:6]}", type="sqlite", config={"database": dynamic_sqlite_db},
        credentials={}, user_token=admin, org_id=org,
    )
    granted = test_client.post(
        f"/api/organizations/{org}/resource-grants", headers=h(admin, org),
        json={"resource_type": "data_source", "resource_id": ds["id"],
              "principal_type": "user", "principal_id": member_id, "permissions": ["query"]},
    )
    assert granted.status_code == 200, granted.text
    key = create_api_key(member, org)["key"]
    assert test_client.get("/api/notifications/count", headers={"X-API-Key": key}).status_code == 200
    assert _row(test_client, admin, org, member_id)["role"] == "admin"

    _remove(test_client, admin, org, row["id"])

    assert test_client.get("/api/notifications/count", headers={"X-API-Key": key}).status_code == 401
    assert _grants(test_client, admin, org, member_id) == []

    readded = invite(test_client, admin_token=admin, org_id=org, email=email, role="member")
    accept_invite(
        test_client, admin_token=admin, org_id=org, membership_id=readded["id"], user_token=member,
    )

    back = _row(test_client, admin, org, member_id)
    assert back["role"] == "member"
    assert [r["name"] for r in back["roles"]] == ["member"]
    assert _grants(test_client, admin, org, member_id) == []
    # The revoked key stays revoked; access is through a fresh sign-in.
    assert test_client.get("/api/notifications/count", headers={"X-API-Key": key}).status_code == 401
    assert is_member(test_client, member, org)


def test_removal_leaves_the_other_organization_untouched(test_client, shared_member, create_api_key):
    o = shared_member
    key_a = create_api_key(o["shared_token"], o["a_org"])["key"]
    key_b = create_api_key(o["shared_token"], o["b_org"])["key"]
    roles_in_a = _row(test_client, o["a_token"], o["a_org"], o["shared_id"])["roles"]

    _remove(
        test_client, o["b_token"], o["b_org"],
        _row(test_client, o["b_token"], o["b_org"], o["shared_id"])["id"],
    )

    assert test_client.get("/api/notifications/count", headers={"X-API-Key": key_b}).status_code == 401
    assert test_client.get("/api/notifications/count", headers={"X-API-Key": key_a}).status_code == 200
    assert _row(test_client, o["a_token"], o["a_org"], o["shared_id"])["roles"] == roles_in_a
    assert is_member(test_client, o["shared_token"], o["a_org"])


async def _restore_membership(org_id, user_id):
    # Direct write: the directory lists the person again.
    from sqlalchemy import update

    from app.models.membership import Membership

    async with async_session_maker() as db:
        await db.execute(
            update(Membership)
            .where(Membership.organization_id == org_id, Membership.user_id == user_id)
            .values(deleted_at=None)
        )
        await db.commit()


def test_directory_retired_member_gets_everything_back_when_the_directory_restores_them(
    test_client, two_orgs, create_api_key,
):
    """A directory sync retiring a membership is reversible: nothing the
    person held is usable meanwhile, and all of it is back on their return."""
    o = two_orgs
    admin, org, member_id = o["a_token"], o["a_org"], o["a_member_id"]
    row = _row(test_client, admin, org, member_id)
    assert test_client.put(
        f"/api/organizations/{org}/members/{row['id']}", json={"role": "admin"}, headers=h(admin, org),
    ).status_code == 200
    key = create_api_key(o["a_member_token"], org)["key"]
    members_with_key = lambda: test_client.get(  # noqa: E731 — needs the admin role
        f"/api/organizations/{org}/members", headers={"X-API-Key": key},
    ).status_code
    assert members_with_key() == 200

    run(_retire_membership(org, member_id))
    assert members_with_key() in (401, 403)
    assert not is_member(test_client, o["a_member_token"], org)

    run(_restore_membership(org, member_id))
    assert members_with_key() == 200
    assert _row(test_client, admin, org, member_id)["role"] == "admin"


async def _retire_membership(org_id, user_id):
    # Direct write: the state a directory (LDAP) sync leaves when the person
    # drops out of every synced group — the row is kept, marked deleted.
    from datetime import datetime

    from sqlalchemy import update

    from app.models.membership import Membership

    async with async_session_maker() as db:
        await db.execute(
            update(Membership)
            .where(Membership.organization_id == org_id, Membership.user_id == user_id)
            .values(deleted_at=datetime.utcnow())
        )
        await db.commit()


def test_retired_membership_no_longer_counts_as_membership(test_client, shared_member, whoami):
    o = shared_member
    usage = lambda: test_client.get(  # noqa: E731
        "/api/license/usage", headers=h(o["b_token"], o["b_org"]),
    ).json()["current_users"]
    seats_before = usage()

    run(_retire_membership(o["b_org"], o["shared_id"]))

    assert {org["id"] for org in whoami(o["shared_token"])["organizations"]} == {o["a_org"]}
    assert not is_member(test_client, o["shared_token"], o["b_org"])
    assert usage() == seats_before - 1
    picker = test_client.get("/api/organization/members", headers=h(o["b_token"], o["b_org"])).json()
    assert o["shared_id"] not in {u["id"] for u in picker}
