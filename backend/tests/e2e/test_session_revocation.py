"""e2e: session tokens are revocable.

Session JWTs used to be stateless with a 7-day lifetime and a logout route that
did nothing server-side, so a bearer token lifted off a user (out of a proxy
log, browser history, or the browser itself) stayed valid for a week no matter
how many times its owner logged out and back in.

Every test here asserts the same invariant from a different angle: once a
session has been revoked, *any* token issued before that point is refused on the
whole authenticated API — while tokens minted afterwards keep working.
"""
import uuid

import pytest

pytestmark = pytest.mark.e2e


def _auth(token, org_id=None):
    headers = {"Authorization": f"Bearer {token}"}
    if org_id:
        headers["X-Organization-Id"] = str(org_id)
    return headers


def _new_account(create_user, login_user):
    email = f"revoke_{uuid.uuid4().hex[:10]}@test.com"
    password = "Test1234!"
    create_user(email=email, password=password)
    return {"email": email, "password": password, "token": login_user(email, password)}


def _org_id(whoami, token):
    """The org registration already put this user in (they land in Main Org)."""
    orgs = whoami(token)["organizations"]
    assert orgs, "expected the registered user to belong to an organization"
    return orgs[0]["id"]


def _token_works(test_client, token) -> bool:
    """Probe an authenticated route; True iff the token still authenticates."""
    resp = test_client.get("/api/users/whoami", headers=_auth(token))
    assert resp.status_code in (200, 401), resp.status_code
    return resp.status_code == 200


def test_logout_revokes_the_token_it_was_called_with(test_client, create_user, login_user):
    acct = _new_account(create_user, login_user)
    assert _token_works(test_client, acct["token"])

    logout = test_client.post("/api/auth/jwt/logout", headers=_auth(acct["token"]))
    assert logout.status_code in (200, 204), logout.status_code

    assert not _token_works(test_client, acct["token"])


def test_logging_back_in_does_not_revive_the_old_token(test_client, create_user, login_user):
    """The reported behavior: log out, log back in, and the old bearer token
    was still accepted. A fresh login must not resurrect a revoked session."""
    acct = _new_account(create_user, login_user)
    stolen = acct["token"]

    test_client.post("/api/auth/jwt/logout", headers=_auth(stolen))
    fresh = login_user(acct["email"], acct["password"])

    assert not _token_works(test_client, stolen)
    assert _token_works(test_client, fresh)


def test_logout_revokes_every_outstanding_session_not_just_the_caller(
    test_client, create_user, login_user
):
    """Sessions on other devices die too — the point of the control is that one
    logout ends every session, including one an attacker holds."""
    acct = _new_account(create_user, login_user)
    other_device = login_user(acct["email"], acct["password"])
    assert _token_works(test_client, other_device)

    test_client.post("/api/auth/jwt/logout", headers=_auth(acct["token"]))

    assert not _token_works(test_client, other_device)


def test_revoked_token_is_refused_across_the_authenticated_api(
    test_client, create_user, login_user, whoami
):
    """Revocation is enforced in the auth dependency, so it holds for every
    authenticated route — not just the one whoami probe the other tests use."""
    acct = _new_account(create_user, login_user)
    org_id = _org_id(whoami, acct["token"])

    routes = [
        "/api/users/whoami",
        "/api/organizations",
        f"/api/organizations/{org_id}/members",
    ]
    for route in routes:
        assert test_client.get(route, headers=_auth(acct["token"], org_id)).status_code == 200, route

    test_client.post("/api/auth/jwt/logout", headers=_auth(acct["token"]))

    for route in routes:
        resp = test_client.get(route, headers=_auth(acct["token"], org_id))
        assert resp.status_code == 401, f"{route} still accepted a revoked token"


def test_password_change_revokes_existing_sessions(test_client, create_user, login_user):
    acct = _new_account(create_user, login_user)
    old_session = login_user(acct["email"], acct["password"])

    resp = test_client.patch(
        "/api/users/me",
        json={"password": "Changed1234!"},
        headers=_auth(acct["token"]),
    )
    assert resp.status_code == 200, resp.json()

    assert not _token_works(test_client, old_session)
    assert not _token_works(test_client, acct["token"])
    assert _token_works(test_client, login_user(acct["email"], "Changed1234!"))


def test_profile_edit_that_is_not_a_password_change_keeps_sessions_alive(
    test_client, create_user, login_user
):
    """Guard against over-revoking: renaming yourself must not sign you out."""
    acct = _new_account(create_user, login_user)

    resp = test_client.patch(
        "/api/users/me",
        json={"name": f"renamed_{uuid.uuid4().hex[:6]}"},
        headers=_auth(acct["token"]),
    )
    assert resp.status_code == 200, resp.json()

    assert _token_works(test_client, acct["token"])


def test_admin_can_force_sign_out_a_member(test_client, create_user, login_user, whoami):
    """The break-glass control for a leaked token: an admin revokes a member's
    sessions without having to deactivate the account."""
    admin = _new_account(create_user, login_user)
    org_id = _org_id(whoami, admin["token"])

    member_email = f"revoke_{uuid.uuid4().hex[:10]}@test.com"
    invite = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers=_auth(admin["token"], org_id),
    )
    assert invite.status_code == 200, invite.json()
    membership_id = invite.json()["id"]

    create_user(email=member_email, password="Test1234!")
    member_token = login_user(member_email, "Test1234!")
    assert _token_works(test_client, member_token)

    resp = test_client.post(
        f"/api/organizations/{org_id}/members/{membership_id}/sign-out",
        headers=_auth(admin["token"], org_id),
    )
    assert resp.status_code == 204, resp.status_code

    assert not _token_works(test_client, member_token)
    # The admin's own session is untouched, and the member can sign back in.
    assert _token_works(test_client, admin["token"])
    assert _token_works(test_client, login_user(member_email, "Test1234!"))


def test_member_cannot_force_sign_out_another_member(
    test_client, create_user, login_user, whoami
):
    """Force-signout is a manage_members action — a regular member must not be
    able to boot anyone, including the admin."""
    admin = _new_account(create_user, login_user)
    org_id = _org_id(whoami, admin["token"])

    admin_membership = test_client.get(
        f"/api/organizations/{org_id}/members", headers=_auth(admin["token"], org_id)
    ).json()[0]["id"]

    member_email = f"revoke_{uuid.uuid4().hex[:10]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers=_auth(admin["token"], org_id),
    )
    create_user(email=member_email, password="Test1234!")
    member_token = login_user(member_email, "Test1234!")

    resp = test_client.post(
        f"/api/organizations/{org_id}/members/{admin_membership}/sign-out",
        headers=_auth(member_token, org_id),
    )
    assert resp.status_code in (401, 403), resp.status_code
    assert _token_works(test_client, admin["token"])
