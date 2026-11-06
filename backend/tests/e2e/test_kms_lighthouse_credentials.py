"""KMS Lighthouse member credentials through the legacy data-source endpoints (P9).

Runtime reads a member's KMS login from the connection-level row only. The legacy
`/api/data_sources/{id}/my-credentials` POST refuses KMS (a row there would say
"Connected" while every browse asks to connect), and its DELETE also clears the
connection-level row, so the integration page's Disconnect really disconnects.
Only the KMS Lighthouse API is simulated (tools/kms_lighthouse mock, in process).
"""
import pytest

from tests.e2e.test_kms_lighthouse import (
    _BOB_FILES, _admin, _assert_connect_required, _file_ids, _member, _user_required_source,
)
from tests.unit.test_kms_lighthouse_client import kms, kms_server  # noqa: F401


def _status(test_client, ds_id, headers):
    """The member's status as the integration page reads it (GET the data source, first connection)."""
    source = test_client.get(f"/api/data_sources/{ds_id}", headers=headers)
    assert source.status_code == 200, source.text
    status = source.json()["connections"][0]["user_status"]
    assert status is not None, source.json()
    return status


@pytest.mark.e2e
def test_legacy_save_refuses_kms_and_stores_nothing(
    kms, test_client, create_user, login_user, whoami, create_data_source,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _user_required_source(kms, test_client, create_data_source, token, org, headers)
    file_endpoint = f"/api/data_sources/{ds_id}/connections/{conn_id}/files"
    bob_h = _member(test_client, create_user, login_user, org, headers, "bob")
    before = _status(test_client, ds_id, bob_h)
    assert before["has_user_credentials"] is False, before

    bob_pass = kms["mock"].USERS["bob"]["password"]
    kms["mock"].REQUEST_LOG.clear()
    r = test_client.post(f"/api/data_sources/{ds_id}/my-credentials",
                         json={"auth_mode": "userpass", "credentials": {"username": "bob", "password": bob_pass}},
                         headers=bob_h)
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["error_code"] == "connection.kms_use_connection_credentials", body
    assert f"/api/connections/{conn_id}/my-credentials" in body["detail"], body
    assert bob_pass not in r.text

    # Nothing was stored: no legacy row, the status is unchanged, and bob still has to connect.
    saved = test_client.get(f"/api/data_sources/{ds_id}/my-credentials", headers=bob_h)
    assert saved.status_code == 200 and saved.json() is None, saved.text
    assert _status(test_client, ds_id, bob_h) == before
    _assert_connect_required(test_client.get(file_endpoint, headers=bob_h))
    assert kms["mock"].REQUEST_LOG == [], "a refused save makes no KMS call"


@pytest.mark.e2e
def test_legacy_disconnect_clears_the_kms_connection_row(
    kms, test_client, create_user, login_user, whoami, create_data_source,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _user_required_source(kms, test_client, create_data_source, token, org, headers)
    file_endpoint = f"/api/data_sources/{ds_id}/connections/{conn_id}/files"
    bob_h = _member(test_client, create_user, login_user, org, headers, "bob")
    alice_h = _member(test_client, create_user, login_user, org, headers, "alice")
    for member_h, credentials in ((bob_h, {"username": "bob", "password": kms["mock"].USERS["bob"]["password"]}),
                                  (alice_h, {"username": "alice", "password": kms["mock"].USERS["alice"]["password"]})):
        r = test_client.post(f"/api/connections/{conn_id}/my-credentials",
                             json={"auth_mode": "userpass", "credentials": credentials}, headers=member_h)
        assert r.status_code == 200, r.text
    assert _file_ids(test_client.get(file_endpoint, headers=bob_h)) == _BOB_FILES
    assert _status(test_client, ds_id, bob_h)["has_user_credentials"] is True

    # The integration page's Disconnect path (the legacy data-source endpoint).
    r = test_client.delete(f"/api/data_sources/{ds_id}/my-credentials", headers=bob_h)
    assert r.status_code == 204, r.text

    kms["mock"].REQUEST_LOG.clear()
    _assert_connect_required(test_client.get(file_endpoint, headers=bob_h))
    assert kms["mock"].REQUEST_LOG == [], "a disconnected member makes no KMS call"
    assert _status(test_client, ds_id, bob_h)["has_user_credentials"] is False
    # Only bob's own login was cleared: alice still reads as herself.
    kms["mock"].REQUEST_LOG.clear()
    assert _file_ids(test_client.get(file_endpoint, headers=alice_h))
    assert {user for user, _, _ in kms["mock"].REQUEST_LOG} == {"alice"}
