"""Adding someone who already has an account elsewhere.

Contract:

* Adding the address of an existing account attaches that account directly,
  whether it belongs to another organization or to none: it becomes a member
  at once, with no invite to accept. Only an instance administrator's account
  still joins by accepting an invite.
* Adding by account id instead of address follows the same rule.
* Member lists show a person's chat identities for the listing organization
  only.
* Signing a member out of every session is an instance-level action when the
  person also belongs to another organization.
* Adding someone does not reactivate an account that another organization's
  directory deactivated.
"""
import uuid

import pytest

from app.dependencies import async_session_maker
from app.settings.config import settings as bow_settings

from ._shared_member import (  # noqa: F401
    get_user_row, h, instance_admin, invite, is_member, multi_org_flag,
    new_email, org_ids, run, set_superuser, set_user_active, shared_member, two_orgs,
)


def _members(test_client, token, org_id):
    resp = test_client.get(f"/api/organizations/{org_id}/members", headers=h(token, org_id))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_adding_another_organizations_member_attaches_them_directly(
    test_client, two_orgs, whoami, multi_org_flag,
):
    o = two_orgs
    email = whoami(o["a_member_token"])["email"]

    added = invite(test_client, admin_token=o["b_token"], org_id=o["b_org"], email=email)

    # A member of both at once; no invite to accept.
    assert added["user_id"] == o["a_member_id"]
    assert org_ids(whoami, o["a_member_token"]) == {o["a_org"], o["b_org"]}
    assert is_member(test_client, o["a_member_token"], o["b_org"])
    row = next(m for m in _members(test_client, o["b_token"], o["b_org"]) if m["id"] == added["id"])
    assert row["user_id"] == o["a_member_id"]
    assert not [
        m for m in _members(test_client, o["b_token"], o["b_org"])
        if (m.get("email") or "").lower() == email.lower() and not m.get("user_id")
    ]

    # Adding again does not stack a second membership.
    again = test_client.post(
        f"/api/organizations/{o['b_org']}/members",
        json={"organization_id": o["b_org"], "email": email, "role": "member"},
        headers=h(o["b_token"], o["b_org"]),
    )
    assert again.status_code == 400


def _remove_from_a(test_client, o):
    row = next(
        m for m in _members(test_client, o["a_token"], o["a_org"]) if m["user_id"] == o["a_member_id"]
    )
    removed = test_client.delete(
        f"/api/organizations/{o['a_org']}/members/{row['id']}", headers=h(o["a_token"], o["a_org"]),
    )
    assert removed.status_code == 204, removed.text


@pytest.mark.parametrize("by", ["email", "user_id"])
def test_account_with_no_organization_is_attached_directly(
    test_client, two_orgs, whoami, multi_org_flag, by,
):
    """Re-adding a removed member attaches the account (and restores its login
    where removal deactivated it), with or without multiple organizations."""
    o = two_orgs
    email = whoami(o["a_member_token"])["email"]
    _remove_from_a(test_client, o)
    body = {"organization_id": o["a_org"], "role": "member"}
    body.update({"email": email} if by == "email" else {"user_id": o["a_member_id"]})

    added = test_client.post(
        f"/api/organizations/{o['a_org']}/members", json=body, headers=h(o["a_token"], o["a_org"]),
    )

    assert added.status_code == 200, added.text
    assert added.json()["user_id"] == o["a_member_id"]
    assert run(get_user_row(o["a_member_id"]))["is_active"] is True
    assert is_member(test_client, o["a_member_token"], o["a_org"])


def test_adding_by_account_id_follows_the_same_rule(test_client, two_orgs, whoami):
    o = two_orgs

    by_id = test_client.post(
        f"/api/organizations/{o['b_org']}/members",
        json={"organization_id": o["b_org"], "user_id": o["a_member_id"], "role": "member"},
        headers=h(o["b_token"], o["b_org"]),
    )

    assert by_id.status_code == 200, by_id.text
    assert by_id.json()["user_id"] == o["a_member_id"]
    assert is_member(test_client, o["a_member_token"], o["b_org"])
    assert is_member(test_client, o["a_member_token"], o["a_org"])


@pytest.mark.parametrize("by", ["email", "user_id"])
def test_org_admin_cannot_add_an_instance_administrator(test_client, instance_admin, multi_org_flag, by):
    o = instance_admin
    before = run(get_user_row(o["admin_id"]))
    body = {"organization_id": o["b_org"], "role": "admin"}
    body.update({"email": o["admin_email"]} if by == "email" else {"user_id": o["admin_id"]})

    resp = test_client.post(
        f"/api/organizations/{o['b_org']}/members", json=body, headers=h(o["b_token"], o["b_org"]),
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == "organization.member_instance_admin"
    assert o["admin_id"] not in {m["user_id"] for m in _members(test_client, o["b_token"], o["b_org"])}
    assert o["admin_email"] not in {m["email"] for m in _members(test_client, o["b_token"], o["b_org"])}
    assert run(get_user_row(o["admin_id"])) == before


def test_instance_administrator_is_invited_not_attached_even_by_another_instance_administrator(
    test_client, instance_admin, multi_org_flag,
):
    o = instance_admin
    run(set_superuser(o["b_id"]))  # B's admin is an instance administrator too

    added = invite(test_client, admin_token=o["b_token"], org_id=o["b_org"], email=o["admin_email"])

    assert added["user_id"] is None
    assert not is_member(test_client, o["admin_token"], o["b_org"])


def test_deactivated_instance_administrator_is_not_reactivated_by_being_added(
    test_client, instance_admin, multi_org_flag,
):
    o = instance_admin
    run(set_superuser(o["b_id"]))
    run(set_user_active(o["admin_id"], False))

    invite(test_client, admin_token=o["b_token"], org_id=o["b_org"], email=o["admin_email"])

    assert run(get_user_row(o["admin_id"]))["is_active"] is False


async def _link_chat_identity(org_id, user_id, external_id):
    # Direct write: a verified chat identity needs a connected Slack/Teams
    # workspace and an inbound message to create through the API.
    from app.models.external_platform import ExternalPlatform
    from app.models.external_user_mapping import ExternalUserMapping

    async with async_session_maker() as db:
        platform = ExternalPlatform(organization_id=org_id, platform_type="slack", platform_config={})
        db.add(platform)
        await db.flush()
        db.add(ExternalUserMapping(
            organization_id=org_id, platform_id=platform.id, platform_type="slack",
            external_user_id=external_id, app_user_id=user_id, is_verified=True,
        ))
        await db.commit()


def test_member_list_shows_only_this_organizations_chat_identities(test_client, shared_member):
    o = shared_member
    in_a, in_b = f"UA{uuid.uuid4().hex[:8]}", f"UB{uuid.uuid4().hex[:8]}"
    run(_link_chat_identity(o["a_org"], o["shared_id"], in_a))
    run(_link_chat_identity(o["b_org"], o["shared_id"], in_b))

    def _identities(token, org_id):
        row = next(m for m in _members(test_client, token, org_id) if m["user_id"] == o["shared_id"])
        return {m["external_user_id"] for m in row["user"]["external_user_mappings"]}

    assert _identities(o["a_token"], o["a_org"]) == {in_a}
    assert _identities(o["b_token"], o["b_org"]) == {in_b}
    # A plain member reading the people picker sees the same boundary.
    picker = test_client.get("/api/organization/members", headers=h(o["shared_token"], o["b_org"]))
    assert picker.status_code == 200, picker.text
    mine = next(u for u in picker.json() if u["id"] == o["shared_id"])
    assert {m["external_user_id"] for m in mine["external_user_mappings"]} == {in_b}


def _sign_out(test_client, token, org_id, membership_id):
    return test_client.post(
        f"/api/organizations/{org_id}/members/{membership_id}/sign-out", headers=h(token, org_id),
    )


def test_org_admin_cannot_sign_out_a_member_who_also_belongs_elsewhere(test_client, shared_member):
    o = shared_member
    rows = _members(test_client, o["b_token"], o["b_org"])
    shared_row = next(m for m in rows if m["user_id"] == o["shared_id"])

    denied = _sign_out(test_client, o["b_token"], o["b_org"], shared_row["id"])

    assert denied.status_code == 403
    assert is_member(test_client, o["shared_token"], o["a_org"])  # session still valid


def test_org_admin_can_sign_out_a_member_of_only_this_organization(test_client, two_orgs):
    o = two_orgs
    row = next(
        m for m in _members(test_client, o["a_token"], o["a_org"]) if m["user_id"] == o["a_member_id"]
    )

    done = _sign_out(test_client, o["a_token"], o["a_org"], row["id"])

    assert done.status_code == 204, done.text
    assert not is_member(test_client, o["a_member_token"], o["a_org"])


def test_sign_out_is_unrestricted_when_multiple_organizations_are_off(
    test_client, shared_member, monkeypatch,
):
    """Deployments that never enabled multiple organizations keep today's behaviour."""
    o = shared_member
    monkeypatch.setattr(bow_settings.bow_config.features, "allow_multiple_organizations", False)
    row = next(
        m for m in _members(test_client, o["b_token"], o["b_org"]) if m["user_id"] == o["shared_id"]
    )

    assert _sign_out(test_client, o["b_token"], o["b_org"], row["id"]).status_code == 204


def test_adding_a_member_does_not_reactivate_an_account_deactivated_elsewhere(
    test_client, two_orgs, whoami,
):
    o = two_orgs
    email = whoami(o["a_member_token"])["email"]
    # Direct write: the state organization A's directory (SCIM) leaves behind.
    run(set_user_active(o["a_member_id"], False))

    invite(test_client, admin_token=o["b_token"], org_id=o["b_org"], email=email)

    assert run(get_user_row(o["a_member_id"]))["is_active"] is False


def test_mcp_request_runs_in_its_organization_context(
    test_client, shared_member, create_api_key, enable_mcp,
):
    """An MCP request is for one organization, like any other request: what
    runs after its authentication sees that organization (response schemas and
    organization guards read it)."""
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from app.core.request_org import current_request_organization_id

    o = shared_member
    enable_mcp(user_token=o["b_token"], org_id=o["b_org"])
    key = create_api_key(o["shared_token"], o["b_org"])["key"]
    seen = []

    def _record(_state):
        seen.append(current_request_organization_id())

    event.listen(Session, "do_orm_execute", _record)
    try:
        resp = test_client.post(
            "/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"X-API-Key": key},
        )
    finally:
        event.remove(Session, "do_orm_execute", _record)

    assert resp.status_code == 200, resp.text
    assert "result" in resp.json()
    # Queries before authentication have no organization; once the principal
    # is established every later one runs for B, never for A.
    assert seen and seen[-1] == o["b_org"]
    assert o["a_org"] not in seen
    first = seen.index(o["b_org"])
    assert set(seen[first:]) == {o["b_org"]}
