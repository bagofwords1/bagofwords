"""SCIM provisioning stays inside the provisioning organization.

Contract: an account row (sign-in address, name, active flag) is shared by
every organization the person belongs to. An organization's identity provider
may write it only while that organization is the person's sole one. For a
person who also belongs elsewhere, SCIM writes are limited to this
organization's membership:

* profile changes are not applied to the account;
* deactivate / delete ends the membership here and leaves the account working
  in the other organization;
* provisioning their address creates an open invite they must accept, not a
  membership;
* ``active: true`` does not reactivate the account.

A person whose only organization is the provisioning one is managed exactly as
before.
"""
import pytest

from ._shared_member import (  # noqa: F401
    accept_invite, accounts_with_email, get_user_row, h, instance_admin, is_member,
    multi_org_flag, new_email, org_ids, run, set_superuser, set_user_active,
    shared_member, two_orgs,
)


def _scim_token(test_client, admin_token, org_id):
    resp = test_client.post(
        "/api/enterprise/scim/tokens", json={"name": "idp"}, headers=h(admin_token, org_id),
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture
def b_scim(test_client, two_orgs):
    return _scim_token(test_client, two_orgs["b_token"], two_orgs["b_org"])


def _provision(test_client, scim, email, **extra):
    return test_client.post(
        "/scim/v2/Users", headers=scim,
        json={"userName": email, "displayName": "Provisioned Person", "active": True, **extra},
    )


def _find(test_client, scim, email):
    resp = test_client.get("/scim/v2/Users", headers=scim, params={"filter": f'userName eq "{email}"'})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _patch(test_client, scim, user_id, value):
    return test_client.patch(
        f"/scim/v2/Users/{user_id}", headers=scim,
        json={"Operations": [{"op": "replace", "value": value}]},
    )


def test_provisioning_another_organizations_member_issues_an_invite(
    test_client, two_orgs, whoami, b_scim,
):
    o = two_orgs
    me = whoami(o["a_member_token"])
    before = run(get_user_row(me["id"]))

    created = _provision(test_client, b_scim, me["email"], externalId="idp-b-1")

    # The identity provider gets a resource it can keep addressing...
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["userName"] == me["email"] and body["active"] is True
    found = _find(test_client, b_scim, me["email"])
    assert found["totalResults"] == 1 and found["Resources"][0]["id"] == body["id"]
    assert test_client.get(f"/scim/v2/Users/{body['id']}", headers=b_scim).status_code == 200
    assert _provision(test_client, b_scim, me["email"]).status_code == 409

    # ...but the person is not in B until they accept, their account is
    # untouched, and B is not shown their profile from A.
    assert not is_member(test_client, o["a_member_token"], o["b_org"])
    assert org_ids(whoami, o["a_member_token"]) == {o["a_org"]}
    assert run(get_user_row(me["id"])) == before
    assert me["name"] not in created.text

    members = test_client.get(
        f"/api/organizations/{o['b_org']}/members", headers=h(o["b_token"], o["b_org"]),
    ).json()
    pending = next(m for m in members if m["email"] == me["email"])
    assert pending["user_id"] is None
    accept_invite(
        test_client, admin_token=o["b_token"], org_id=o["b_org"],
        membership_id=pending["id"], user_token=o["a_member_token"],
    )
    assert is_member(test_client, o["a_member_token"], o["b_org"])


def test_withdrawing_a_pending_provision_removes_the_invite(test_client, two_orgs, whoami, b_scim):
    o = two_orgs
    email = whoami(o["a_member_token"])["email"]
    user_id = _provision(test_client, b_scim, email).json()["id"]

    assert test_client.delete(f"/scim/v2/Users/{user_id}", headers=b_scim).status_code == 204

    assert _find(test_client, b_scim, email)["totalResults"] == 0
    assert run(get_user_row(user_id))["is_active"] is True
    assert is_member(test_client, o["a_member_token"], o["a_org"])


@pytest.mark.parametrize("how", ["patch", "put", "delete"])
def test_deprovisioning_a_shared_account_only_ends_this_organizations_membership(
    test_client, shared_member, b_scim, how,
):
    o = shared_member
    uid = o["shared_id"]

    if how == "patch":
        resp = _patch(test_client, b_scim, uid, {"active": False})
        assert resp.status_code == 200 and resp.json()["active"] is False, resp.text
    elif how == "put":
        resp = test_client.put(
            f"/scim/v2/Users/{uid}", headers=b_scim,
            json={"userName": o["shared_email"], "active": False},
        )
        assert resp.status_code == 200 and resp.json()["active"] is False, resp.text
    else:
        assert test_client.delete(f"/scim/v2/Users/{uid}", headers=b_scim).status_code == 204

    assert not is_member(test_client, o["shared_token"], o["b_org"])
    assert test_client.get(f"/scim/v2/Users/{uid}", headers=b_scim).status_code == 404
    # Organization A is unaffected: same session, account still active.
    assert is_member(test_client, o["shared_token"], o["a_org"])
    assert run(get_user_row(uid))["is_active"] is True


@pytest.mark.parametrize("how", ["patch", "put"])
def test_profile_writes_do_not_reach_a_shared_account(test_client, shared_member, b_scim, how):
    o = shared_member
    uid = o["shared_id"]
    before = run(get_user_row(uid))
    other = new_email("renamed")

    if how == "patch":
        resp = _patch(test_client, b_scim, uid, {"userName": other, "displayName": "Someone Else"})
    else:
        resp = test_client.put(
            f"/scim/v2/Users/{uid}", headers=b_scim,
            json={"userName": other, "displayName": "Someone Else", "active": True},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["userName"] == before["email"]  # reflects what is stored
    assert run(get_user_row(uid)) == before
    assert is_member(test_client, o["shared_token"], o["b_org"])


def test_activating_does_not_reactivate_a_shared_account(test_client, shared_member, b_scim):
    o = shared_member
    # Direct write: the state another organization's directory leaves behind.
    run(set_user_active(o["shared_id"], False))

    resp = _patch(test_client, b_scim, o["shared_id"], {"active": True})

    assert resp.status_code == 200, resp.text
    assert run(get_user_row(o["shared_id"]))["is_active"] is False


def test_sole_organization_account_is_still_fully_managed(test_client, two_orgs, b_scim):
    """The single-organization behaviour: profile and active flag are written."""
    email, renamed = new_email("solo"), new_email("solo_renamed")
    created = _provision(test_client, b_scim, email)
    assert created.status_code == 201, created.text
    uid = created.json()["id"]

    resp = _patch(test_client, b_scim, uid, {"userName": renamed, "displayName": "New Name"})
    assert resp.status_code == 200, resp.text
    assert run(get_user_row(uid)) == {"email": renamed, "name": "New Name", "is_active": True}

    assert _patch(test_client, b_scim, uid, {"active": False}).json()["active"] is False
    assert run(get_user_row(uid))["is_active"] is False
    # Deactivated, not removed: the identity provider can still address and restore them.
    assert _patch(test_client, b_scim, uid, {"active": True}).json()["active"] is True
    assert run(get_user_row(uid))["is_active"] is True

    assert test_client.delete(f"/scim/v2/Users/{uid}", headers=b_scim).status_code == 204
    assert run(get_user_row(uid))["is_active"] is False
    assert test_client.get(f"/scim/v2/Users/{uid}", headers=b_scim).status_code == 200


def test_reprovisioning_an_account_with_no_organization_attaches_only_on_single_organization_deployments(
    test_client, two_orgs, whoami, b_scim, multi_org_flag,
):
    o = two_orgs
    me = whoami(o["a_member_token"])
    rows = test_client.get(
        f"/api/organizations/{o['a_org']}/members", headers=h(o["a_token"], o["a_org"]),
    ).json()
    row = next(m for m in rows if m["user_id"] == me["id"])
    assert test_client.delete(
        f"/api/organizations/{o['a_org']}/members/{row['id']}", headers=h(o["a_token"], o["a_org"]),
    ).status_code == 204

    created = _provision(test_client, b_scim, me["email"])

    assert created.status_code == 201, created.text
    assert is_member(test_client, o["a_member_token"], o["b_org"]) is (not multi_org_flag)
    if multi_org_flag:
        assert me["name"] not in created.text  # an invite, not their profile


def test_inactive_provision_of_another_organizations_account_returns_nothing_about_it(
    test_client, two_orgs, whoami, b_scim,
):
    o = two_orgs
    me = whoami(o["a_member_token"])

    resp = test_client.post(
        "/scim/v2/Users", headers=b_scim, json={"userName": me["email"], "active": False},
    )

    assert resp.status_code == 409, resp.text
    assert me["id"] not in resp.text and me["name"] not in resp.text
    assert _find(test_client, b_scim, me["email"])["totalResults"] == 0
    assert not is_member(test_client, o["a_member_token"], o["b_org"])


def test_case_variant_of_a_registered_address_is_the_same_account(test_client, two_orgs, whoami, b_scim):
    o = two_orgs
    solo = new_email("solo")
    assert _provision(test_client, b_scim, solo).status_code == 201
    theirs = whoami(o["a_member_token"])["email"]

    again = _provision(test_client, b_scim, solo.upper())
    other = _provision(test_client, b_scim, theirs.upper())

    assert again.status_code == 409, again.text
    assert run(accounts_with_email(solo)) == 1
    # Another organization's member under a case variant: an invite, not a new account.
    assert other.status_code == 201 and other.json()["id"] == o["a_member_id"], other.text
    assert run(accounts_with_email(theirs)) == 1
    assert not is_member(test_client, o["a_member_token"], o["b_org"])


def test_instance_administrator_cannot_be_provisioned(test_client, instance_admin, b_scim, multi_org_flag):
    o = instance_admin
    before = run(get_user_row(o["admin_id"]))

    resp = _provision(test_client, b_scim, o["admin_email"], externalId="idp-x")

    assert resp.status_code == 409, resp.text
    assert o["admin_id"] not in resp.text and before["name"] not in resp.text
    assert run(get_user_row(o["admin_id"])) == before
    assert not is_member(test_client, o["admin_token"], o["b_org"])
    members = test_client.get(
        f"/api/organizations/{o['b_org']}/members", headers=h(o["b_token"], o["b_org"]),
    ).json()
    assert o["admin_email"] not in {m["email"] for m in members}


@pytest.mark.parametrize("how", ["patch", "put", "delete"])
def test_instance_administrator_account_is_never_written(
    test_client, two_orgs, b_scim, multi_org_flag, how,
):
    """Even when this organization is the account's only one."""
    email = new_email("solo")
    uid = _provision(test_client, b_scim, email).json()["id"]
    run(set_superuser(uid))
    before = run(get_user_row(uid))
    other = new_email("renamed")

    if how == "patch":
        resp = _patch(test_client, b_scim, uid, {"userName": other, "displayName": "Someone Else"})
    elif how == "put":
        resp = test_client.put(
            f"/scim/v2/Users/{uid}", headers=b_scim,
            json={"userName": other, "displayName": "Someone Else", "active": False},
        )
    else:
        resp = test_client.delete(f"/scim/v2/Users/{uid}", headers=b_scim)

    assert resp.status_code in (200, 204), resp.text
    assert run(get_user_row(uid)) == before


def test_deactivated_instance_administrator_is_not_reactivated(test_client, two_orgs, b_scim):
    uid = _provision(test_client, b_scim, new_email("solo")).json()["id"]
    run(set_superuser(uid))
    run(set_user_active(uid, False))

    resp = _patch(test_client, b_scim, uid, {"active": True})

    assert resp.status_code == 200, resp.text
    assert run(get_user_row(uid))["is_active"] is False


def test_open_invites_are_not_scim_resources_on_a_single_organization_deployment(
    test_client, two_orgs, whoami, b_scim, monkeypatch,
):
    """With multiple organizations off, list and get see members only, exactly
    as they always did."""
    from app.settings.config import settings as bow_settings

    o = two_orgs
    email = whoami(o["a_member_token"])["email"]
    uid = _provision(test_client, b_scim, email).json()["id"]
    assert _find(test_client, b_scim, email)["totalResults"] == 1

    monkeypatch.setattr(bow_settings.bow_config.features, "allow_multiple_organizations", False)

    assert _find(test_client, b_scim, email)["totalResults"] == 0
    assert test_client.get(f"/scim/v2/Users/{uid}", headers=b_scim).status_code == 404
    assert _patch(test_client, b_scim, uid, {"active": False}).status_code == 404
