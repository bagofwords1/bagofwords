"""Saving a KMS Lighthouse member login checks it with KMS first.

`POST /api/connections/{id}/my-credentials` used to store any login that passed the
schema, so a wrong password or token read as "Connected" until the Files tab failed.
Now a login KMS rejects is refused with a typed 422 and nothing is stored; a KMS that
cannot be reached (or fails for any reason other than the login) still saves, as
before; and the member's status carries the identity KMS reported, never the BOW
user's name. Only the KMS Lighthouse API is simulated (tools/kms_lighthouse mock).
"""
import socket

import pytest

from tests.e2e.test_kms_lighthouse import (
    _BOB_FILES, _admin, _assert_connect_required, _cfg, _file_ids, _login, _member, _svc_userpass,
    _user_required_source,
)
from tests.e2e.test_kms_lighthouse_credentials import _status
from tests.unit.test_kms_lighthouse_client import kms, kms_server  # noqa: F401

_REJECTED = "connection.kms_login_rejected"
_PAUSED = "connection.kms_login_paused"


def _setup(kms, test_client, create_user, login_user, whoami, create_data_source, member="bob"):
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _user_required_source(kms, test_client, create_data_source, token, org, headers)
    member_h = _member(test_client, create_user, login_user, org, headers, member)
    return ds_id, conn_id, member_h


def _save(test_client, conn_id, headers, auth_mode, credentials):
    return test_client.post(f"/api/connections/{conn_id}/my-credentials",
                            json={"auth_mode": auth_mode, "credentials": credentials}, headers=headers)


def _logins(kms, user):
    return [row for row in kms["mock"].REQUEST_LOG if row == (user, "POST", _login(kms))]


@pytest.mark.e2e
@pytest.mark.parametrize("auth_mode,credentials,secret", [
    ("userpass", {"username": "bob", "password": "wrong-kms-pass-save-5d1"}, "wrong-kms-pass-save-5d1"),
    ("token", {"api_token": "not-a-kms-token-8c2e"}, "not-a-kms-token-8c2e"),
], ids=["userpass", "token"])
def test_a_login_kms_rejects_is_refused_and_nothing_is_stored(
    kms, test_client, create_user, login_user, whoami, create_data_source, auth_mode, credentials, secret,
):
    ds_id, conn_id, bob_h = _setup(kms, test_client, create_user, login_user, whoami, create_data_source)
    kms["mock"].REQUEST_LOG.clear()

    r = _save(test_client, conn_id, bob_h, auth_mode, credentials)

    assert r.status_code == 422, r.text
    assert r.json()["error_code"] == _REJECTED, r.json()
    assert secret not in r.text
    assert kms["mock"].REQUEST_LOG, "the login was checked with KMS"
    status = _status(test_client, ds_id, bob_h)
    assert status["has_user_credentials"] is False, status
    kms["mock"].REQUEST_LOG.clear()
    _assert_connect_required(test_client.get(f"/api/data_sources/{ds_id}/connections/{conn_id}/files", headers=bob_h))
    assert kms["mock"].REQUEST_LOG == [], "a refused save leaves the member unconnected"


@pytest.mark.e2e
@pytest.mark.parametrize("switch,value", [("session_expiry", "immediate"), ("login_mode", "none")],
                         ids=["session_refused", "no_session"])
def test_a_login_kms_accepts_without_a_usable_session_is_refused_as_a_session_problem(
    kms, test_client, create_user, login_user, whoami, create_data_source, switch, value,
):
    """KMS took the password but refused (or never gave) the session: not a wrong password, so the
    member is not told to check it; nothing is stored either."""
    ds_id, conn_id, bob_h = _setup(kms, test_client, create_user, login_user, whoami, create_data_source)
    kms["mock"].STATE[switch] = value
    bob_pass = kms["mock"].USERS["bob"]["password"]

    r = _save(test_client, conn_id, bob_h, "userpass", {"username": "bob", "password": bob_pass})

    assert r.status_code == 422, r.text
    assert r.json()["error_code"] == "connection.kms_session_refused", r.json()
    assert bob_pass not in r.text
    assert kms["mock"].STATE["accepted_logins"] >= 1, "KMS accepted the password"
    assert _status(test_client, ds_id, bob_h)["has_user_credentials"] is False


@pytest.mark.e2e
def test_a_refused_login_keeps_the_login_saved_before_it(
    kms, test_client, create_user, login_user, whoami, create_data_source,
):
    ds_id, conn_id, bob_h = _setup(kms, test_client, create_user, login_user, whoami, create_data_source)
    good = {"username": "bob", "password": kms["mock"].USERS["bob"]["password"]}
    assert _save(test_client, conn_id, bob_h, "userpass", good).status_code == 200

    r = _save(test_client, conn_id, bob_h, "userpass", {"username": "bob", "password": "typo-kms-pass-71"})

    assert r.status_code == 422 and r.json()["error_code"] == _REJECTED, r.text
    assert _file_ids(test_client.get(f"/api/data_sources/{ds_id}/connections/{conn_id}/files",
                                     headers=bob_h)) == _BOB_FILES
    assert _status(test_client, ds_id, bob_h)["account_identity"] == kms["mock"].USERS["bob"]["session_username"]


@pytest.mark.e2e
def test_saving_the_same_rejected_password_again_sends_no_second_login(
    kms, test_client, create_user, login_user, whoami, create_data_source,
):
    """The client's login hold (300 s) covers Save too, so retrying cannot lock the KMS account."""
    ds_id, conn_id, bob_h = _setup(kms, test_client, create_user, login_user, whoami, create_data_source)
    wrong = {"username": "bob", "password": "wrong-twice-kms-3a"}
    kms["mock"].REQUEST_LOG.clear()

    first = _save(test_client, conn_id, bob_h, "userpass", wrong)
    second = _save(test_client, conn_id, bob_h, "userpass", wrong)

    assert first.status_code == 422 and first.json()["error_code"] == _REJECTED, first.text
    assert second.status_code == 422 and second.json()["error_code"] == _PAUSED, second.text
    assert "wrong-twice-kms-3a" not in second.text
    assert len(_logins(kms, "bob")) == 1, kms["mock"].REQUEST_LOG
    # The hold is for that password only: the right one saves at once.
    good = {"username": "bob", "password": kms["mock"].USERS["bob"]["password"]}
    assert _save(test_client, conn_id, bob_h, "userpass", good).status_code == 200
    assert _status(test_client, ds_id, bob_h)["has_user_credentials"] is True


@pytest.mark.e2e
@pytest.mark.parametrize("member,auth_mode", [("bob", "userpass"), ("alice", "token")])
def test_a_good_save_reports_the_identity_kms_reported(
    kms, test_client, create_user, login_user, whoami, create_data_source, member, auth_mode,
):
    ds_id, conn_id, member_h = _setup(kms, test_client, create_user, login_user, whoami, create_data_source, member)
    mock = kms["mock"]
    credentials = ({"username": member, "password": mock.USERS[member]["password"]} if auth_mode == "userpass"
                   else {"api_token": next(t for t, u in mock.TOKENS.items() if u == member)})
    reported = mock.USERS[member]["session_username"]

    r = _save(test_client, conn_id, member_h, auth_mode, credentials)

    assert r.status_code == 200, r.text
    assert r.json()["verified"] is True and r.json()["identity"] == reported, r.json()
    status = _status(test_client, ds_id, member_h)
    assert status["has_user_credentials"] is True
    assert status["account_identity"] == reported, status


@pytest.mark.e2e
def test_a_token_without_a_reported_identity_shows_no_name(
    kms, test_client, create_user, login_user, whoami, create_data_source,
):
    ds_id, conn_id, alice_h = _setup(kms, test_client, create_user, login_user, whoami, create_data_source, "alice")
    kms["mock"].STATE["session_info_status"] = 500

    r = _save(test_client, conn_id, alice_h, "token", {"api_token": "alice-api-token-1"})

    assert r.status_code == 200 and r.json()["verified"] is True, r.text
    assert r.json()["identity"] is None, r.json()
    assert _status(test_client, ds_id, alice_h)["account_identity"] is None


@pytest.mark.e2e
@pytest.mark.parametrize("trouble", ["unreachable", "login_server_error"])
def test_a_kms_that_cannot_check_the_login_still_lets_the_member_save(
    kms, test_client, create_user, login_user, whoami, create_data_source, trouble,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    if trouble == "unreachable":
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]
        ds = create_data_source(name="KMS offline", type="kms_lighthouse",
                                config=_cfg(kms, base_url=f"http://127.0.0.1:{closed_port}", root_folder_id=10),
                                credentials=_svc_userpass(kms), auth_policy="user_required", user_token=token, org_id=org)
        assert test_client.put(f"/api/data_sources/{ds['id']}", json={"is_public": True}, headers=headers).status_code == 200
        ds_id = ds["id"]
        conn_id = test_client.get(f"/api/data_sources/{ds_id}/connections", headers=headers).json()[0]["id"]
    else:
        ds_id, conn_id = _user_required_source(kms, test_client, create_data_source, token, org, headers)
        kms["mock"].STATE["login_status"] = 503
    bob_h = _member(test_client, create_user, login_user, org, headers, "bob")

    r = _save(test_client, conn_id, bob_h, "userpass", {"username": "bob", "password": "any-kms-pass-0f"})

    assert r.status_code == 200, r.text
    assert r.json()["verified"] is False and r.json()["identity"] is None, r.json()
    assert "any-kms-pass-0f" not in r.text
    status = _status(test_client, ds_id, bob_h)
    assert status["has_user_credentials"] is True, status
    assert status["account_identity"] is None, status
