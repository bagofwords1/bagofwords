"""What a user may change about their own account through PATCH /api/users/me.

Contract:
* The display name is self-editable.
* The login email is not: it is the key invites and first sign-in match on.
* A new password is refused when the deployment signs in through SSO only;
  a ``current_password`` sent along with it must be correct.
The same rules hold for an organization admin and for a plain member.
"""
import uuid

import pytest

from tests.e2e.org_isolation.identity_helpers import (
    PASSWORD, auth_mode, bootstrap_admin, hdr, invite, login, register, unique_email, whoami,
)


@pytest.fixture(params=["admin", "member"])
def account(request, test_client):
    admin = bootstrap_admin(test_client)
    if request.param == "admin":
        return admin
    email = unique_email("member")
    _, token = invite(test_client, admin["token"], admin["org_id"], email)
    assert register(test_client, email, invite_token=token).status_code == 201
    return {"email": email, "token": login(test_client, email).json()["access_token"]}


def _patch_me(client, account, body):
    return client.patch("/api/users/me", json=body, headers=hdr(account["token"]))


def test_display_name_is_self_editable(test_client, account):
    new_name = f"renamed {uuid.uuid4().hex[:6]}"
    resp = _patch_me(test_client, account, {"name": new_name})
    assert resp.status_code == 200, resp.text
    assert whoami(test_client, account["token"])["name"] == new_name


def test_login_email_cannot_be_changed_by_its_owner(test_client, account):
    wanted = unique_email("someone_else")

    resp = _patch_me(test_client, account, {"email": wanted})

    assert resp.status_code == 400, resp.text
    assert whoami(test_client, account["token"])["email"] == account["email"]
    assert login(test_client, wanted).status_code == 400
    assert login(test_client, account["email"]).status_code == 200
    # The address is still free for the person it belongs to.
    with_name = _patch_me(test_client, account, {"name": "x", "email": wanted})
    assert with_name.status_code == 400 and whoami(test_client, account["token"])["name"] != "x"


def test_resending_the_current_email_with_a_name_change_is_accepted(test_client, account):
    resp = _patch_me(test_client, account, {"name": "Same Address", "email": account["email"]})
    assert resp.status_code == 200, resp.text
    assert whoami(test_client, account["token"])["name"] == "Same Address"


def test_wrong_current_password_refuses_the_change(test_client, account):
    new_password = f"New-{uuid.uuid4().hex[:10]}"

    resp = _patch_me(test_client, account, {"password": new_password, "current_password": "not-the-password"})

    assert resp.status_code == 400, resp.text
    assert login(test_client, account["email"], new_password).status_code == 400
    assert login(test_client, account["email"], PASSWORD).status_code == 200


def test_password_change_without_current_password_still_works(test_client, account):
    """Existing API clients send only the new password."""
    new_password = f"New-{uuid.uuid4().hex[:10]}"
    resp = _patch_me(test_client, account, {"password": new_password})
    assert resp.status_code == 200, resp.text
    assert login(test_client, account["email"], new_password).status_code == 200


def test_password_change_with_the_current_password_works(test_client, account):
    new_password = f"New-{uuid.uuid4().hex[:10]}"

    resp = _patch_me(test_client, account, {"password": new_password, "current_password": PASSWORD})

    assert resp.status_code == 200, resp.text
    assert "current_password" not in resp.json()
    assert login(test_client, account["email"], new_password).status_code == 200
    assert login(test_client, account["email"], PASSWORD).status_code == 400


def test_password_cannot_be_set_when_sign_in_is_sso_only(test_client, account):
    new_password = f"New-{uuid.uuid4().hex[:10]}"
    with auth_mode("sso_only"):
        resp = _patch_me(test_client, account, {"password": new_password, "current_password": PASSWORD})
        renamed = _patch_me(test_client, account, {"name": "Still Editable"})
    assert resp.status_code == 400, resp.text
    assert renamed.status_code == 200, renamed.text
    assert login(test_client, account["email"], new_password).status_code == 400
    assert login(test_client, account["email"], PASSWORD).status_code == 200
