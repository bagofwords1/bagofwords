"""Real connection API/DB flow for the KMS Lighthouse connector; only the KMS
Lighthouse API is simulated (tools/kms_lighthouse mock, an in-process server)."""
import socket
import uuid

import pytest

from tests.unit.test_kms_lighthouse_client import kms, kms_server  # noqa: F401


def _cfg(kms, auth_type="userpass", **extra):
    return {"base_url": kms["base_url"], "allow_http": True, "index_mode": "none", "auth_type": auth_type, **extra}


def _svc_userpass(kms):
    return {"username": "svc", "password": kms["mock"].USERS["svc"]["password"]}


def _login(kms):
    return kms["mock"].PREFIX + "/login"


def _users_seen(kms):
    """Every identity the mock KMS saw (a login names its user; other calls name the session's user)."""
    return {user for user, _, _ in kms["mock"].REQUEST_LOG}


def _admin(create_user, login_user, whoami):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org = whoami(token)["organizations"][0]["id"]
    return token, org, {"Authorization": f"Bearer {token}", "X-Organization-Id": org}


def _invite(test_client, create_user, login_user, org, admin_headers, login, role="member"):
    """Invite a user with `role`; returns (token, headers, membership id)."""
    email = f"kms-{login}-{uuid.uuid4().hex[:6]}@example.com"
    r = test_client.post(f"/api/organizations/{org}/members", json={"organization_id": org, "email": email, "role": role},
                         headers=admin_headers)
    assert r.status_code == 200, r.json()
    membership_id = r.json()["id"]
    user = create_user(email=email)
    token = login_user(user["email"], user["password"])
    return token, {"Authorization": f"Bearer {token}", "X-Organization-Id": org}, membership_id


def _member(test_client, create_user, login_user, org, admin_headers, login):
    return _invite(test_client, create_user, login_user, org, admin_headers, login)[1]


def _user_required_source(kms, test_client, create_data_source, token, org, headers):
    """A public user_required KMS data source rooted at Procedures, with svc system credentials."""
    ds = create_data_source(name="KMS per-user", type="kms_lighthouse", config=_cfg(kms, root_folder_id=10),
                            credentials=_svc_userpass(kms), auth_policy="user_required", user_token=token, org_id=org)
    assert test_client.put(f"/api/data_sources/{ds['id']}", json={"is_public": True}, headers=headers).status_code == 200
    connections = test_client.get(f"/api/data_sources/{ds['id']}/connections", headers=headers).json()
    return ds["id"], connections[0]["id"]


def _file_ids(response):
    assert response.status_code == 200, response.json()
    body = response.json()
    assert not body.get("connect_required"), body
    return {f["id"] for f in body["files"]}


def _assert_connect_required(response):
    """The structured connect-required state: 200, the flag set, and no files."""
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("connect_required") is True, body
    assert body.get("files", []) == [], body


# From the mock tree under Procedures (root 10), online only: bob's visible set holds 301 and,
# in Billing, 302. alice and svc see every online file there: Policy 305, the Billing scenario
# 4401 and the two Forms items 303/304 too. 306 and the "Old forms" folder are offline (hidden).
_BOB_FILES = {"item:301", "item:302"}
_ROOT_FILES = _BOB_FILES | {"item:305", "scenario:4401", "item:303", "item:304"}
# Outside root 10: 279 under Accounts (alice and svc see it), 307 under Restricted (alice only).
_OUT_OF_ROOT = {"item:279", "item:307"}


def _assert_root_scoped(ids):
    assert not ids & _OUT_OF_ROOT, f"files from outside root 10 leaked: {sorted(ids & _OUT_OF_ROOT)}"
    assert ids == _ROOT_FILES, sorted(ids)


@pytest.mark.e2e
def test_connection_lifecycle_and_secrets(
    kms, test_client, create_user, login_user, whoami, create_connection, get_connection, test_connection_connectivity,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    secret = kms["mock"].USERS["svc"]["password"]
    conn = create_connection(name="KMS QA", type="kms_lighthouse", config=_cfg(kms), credentials=_svc_userpass(kms),
                             user_token=token, org_id=org)
    assert conn["type"] == "kms_lighthouse"
    details = get_connection(connection_id=conn["id"], user_token=token, org_id=org)
    assert secret not in str(details)
    probe = test_connection_connectivity(connection_id=conn["id"], user_token=token, org_id=org)
    assert probe["success"] is True, probe

    wrong = "wrong-kms-pass-e2e-41c7"
    kms["mock"].REQUEST_LOG.clear()
    r = test_client.post("/api/connections/test-params", json={"name": "Bad password", "type": "kms_lighthouse", "config": _cfg(kms),
                                                               "credentials": {"username": "svc", "password": wrong}}, headers=headers)
    assert r.status_code == 200 and r.json()["success"] is False, r.json()
    assert wrong not in r.text
    # The failure is the KMS rejecting the login, not an egress or config error before it.
    assert ("svc", "POST", _login(kms)) in kms["mock"].REQUEST_LOG, kms["mock"].REQUEST_LOG

    member_headers = _member(test_client, create_user, login_user, org, headers, "member")
    changed = test_client.put(f"/api/connections/{conn['id']}", json={"name": "Unauthorized change"}, headers=member_headers)
    assert changed.status_code == 403
    assert secret not in changed.text


@pytest.mark.e2e
def test_switching_auth_variant_uses_the_new_one(
    kms, test_client, create_user, login_user, whoami, create_connection, test_connection_connectivity,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    conn = create_connection(name="KMS token", type="kms_lighthouse", config=_cfg(kms, auth_type="token"),
                             credentials={"api_token": "alice-api-token-1"}, user_token=token, org_id=org)
    # The stored credentials blob keeps the old api_token (alice's) next to the new login (svc's),
    # so any call made with the stale token would show up as alice.
    r = test_client.put(f"/api/connections/{conn['id']}", json={"config": _cfg(kms, auth_type="userpass"),
                                                                "credentials": _svc_userpass(kms)}, headers=headers)
    assert r.status_code == 200, r.json()
    kms["mock"].REQUEST_LOG.clear()
    probe = test_connection_connectivity(connection_id=conn["id"], user_token=token, org_id=org)
    assert probe["success"] is True, probe
    assert ("svc", "POST", _login(kms)) in kms["mock"].REQUEST_LOG, "the userpass variant must log in as svc"
    assert _users_seen(kms) == {"svc"}, "no call may run with the stale token"


@pytest.mark.e2e
def test_user_required_members_read_as_themselves(
    kms, test_client, create_user, login_user, whoami, create_data_source,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _user_required_source(kms, test_client, create_data_source, token, org, headers)
    fields = test_client.get("/api/data_sources/kms_lighthouse/fields", params={"auth_policy": "user_required"}, headers=headers).json()
    assert set(fields["credentials_by_auth"]) == {"userpass", "token"}

    file_endpoint = f"/api/data_sources/{ds_id}/connections/{conn_id}/files"
    bob_h = _member(test_client, create_user, login_user, org, headers, "bob")
    alice_h = _member(test_client, create_user, login_user, org, headers, "alice")
    kms["mock"].REQUEST_LOG.clear()
    _assert_connect_required(test_client.get(file_endpoint, headers=bob_h))
    assert kms["mock"].REQUEST_LOG == [], "no KMS call may run before the member connects"

    endpoint = f"/api/connections/{conn_id}/my-credentials"
    r = test_client.post(endpoint, json={"auth_mode": "userpass", "credentials": {"username": "bob", "password": ""}}, headers=bob_h)
    assert r.status_code == 422, r.json()
    # The rejected save stored nothing: bob is still not connected.
    _assert_connect_required(test_client.get(file_endpoint, headers=bob_h))
    assert kms["mock"].REQUEST_LOG == []
    bob_pass = kms["mock"].USERS["bob"]["password"]
    r = test_client.post(endpoint, json={"auth_mode": "userpass", "credentials": {"username": "bob", "password": bob_pass}}, headers=bob_h)
    assert r.status_code == 200, r.json()
    assert bob_pass not in r.text
    alice_token = "alice-api-token-1"
    r = test_client.post(endpoint, json={"auth_mode": "token", "credentials": {"api_token": alice_token}}, headers=alice_h)
    assert r.status_code == 200, r.json()
    assert alice_token not in r.text
    probe = test_client.post(f"/api/connections/{conn_id}/test-my-credentials", headers=bob_h)
    assert probe.status_code == 200 and probe.json()["success"] is True, probe.json()

    kms["mock"].REQUEST_LOG.clear()
    assert _file_ids(test_client.get(file_endpoint, headers=bob_h)) == _BOB_FILES
    assert _users_seen(kms) == {"bob"}, "every call must run as bob"

    kms["mock"].REQUEST_LOG.clear()
    _assert_root_scoped(_file_ids(test_client.get(file_endpoint, headers=alice_h)))
    assert _users_seen(kms) == {"alice"}, "every call must run as alice"

    r = test_client.delete(endpoint, headers=bob_h)
    assert r.status_code in (200, 204), r.text
    kms["mock"].REQUEST_LOG.clear()
    _assert_connect_required(test_client.get(file_endpoint, headers=bob_h))
    assert kms["mock"].REQUEST_LOG == []


@pytest.mark.e2e
def test_owner_without_a_row_runs_as_the_connection_identity(
    kms, test_client, create_user, login_user, whoami, create_data_source,
):
    """Admin fallback (A6): an org admin who owns the data source, with no saved row, runs as the
    system credentials, because they are an admin. Ownership alone grants nothing (next test)."""
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _user_required_source(kms, test_client, create_data_source, token, org, headers)
    file_endpoint = f"/api/data_sources/{ds_id}/connections/{conn_id}/files"

    kms["mock"].REQUEST_LOG.clear()
    _assert_root_scoped(_file_ids(test_client.get(file_endpoint, headers=headers)))
    assert _users_seen(kms) == {"svc"}, "the owner must run as the connection identity"

    member_h = _member(test_client, create_user, login_user, org, headers, "plain")
    kms["mock"].REQUEST_LOG.clear()
    _assert_connect_required(test_client.get(file_endpoint, headers=member_h))
    assert kms["mock"].REQUEST_LOG == [], "a member without a row makes no KMS call"


@pytest.mark.e2e
def test_owner_who_is_not_an_admin_connects_with_their_own_login(
    kms, test_client, create_user, login_user, whoami, create_data_source,
):
    """Owning a data source never unlocks the shared account. A member needs create_data_source to
    own one, which only an admin holds, so the owner builds the data source as an admin and is then
    demoted."""
    _, org, headers = _admin(create_user, login_user, whoami)
    owner_token, owner_h, owner_membership = _invite(test_client, create_user, login_user, org, headers, "owner", role="admin")
    ds_id, conn_id = _user_required_source(kms, test_client, create_data_source, owner_token, org, owner_h)
    demoted = test_client.put(f"/api/organizations/{org}/members/{owner_membership}", json={"role": "member"}, headers=headers)
    assert demoted.status_code == 200 and demoted.json()["role"] == "member", demoted.json()
    # The demotion took effect, so the owner can no longer pass through the admin branch, which
    # needs org-level manage_connections. test-params is gated on that org permission alone; the
    # connection PUT is not a fit, since the creator keeps a per-connection manage grant.
    probe = test_client.post("/api/connections/test-params", json={"name": "Owner probe", "type": "kms_lighthouse",
                                                                   "config": _cfg(kms), "credentials": _svc_userpass(kms)},
                             headers=owner_h)
    assert probe.status_code == 403, probe.text
    file_endpoint = f"/api/data_sources/{ds_id}/connections/{conn_id}/files"

    kms["mock"].REQUEST_LOG.clear()
    _assert_connect_required(test_client.get(file_endpoint, headers=owner_h))
    # These two load the connection fresh, the case where ownership used to unlock the shared login.
    test_client.post(f"/api/connections/{conn_id}/test-my-credentials", headers=owner_h)
    test_client.get(f"/api/data_sources/{ds_id}/test_connection", headers=owner_h)
    assert kms["mock"].REQUEST_LOG == [], "the owner must not reach KMS before connecting"

    # The status has to say so too: the UI shows Connect only when it is not "system".
    source = test_client.get(f"/api/data_sources/{ds_id}", headers=owner_h)
    assert source.status_code == 200, source.text
    status = source.json()["connections"][0]["user_status"]
    assert status["effective_auth"] == "none", status
    assert status["uses_fallback"] is False, status

    bob_pass = kms["mock"].USERS["bob"]["password"]
    saved = test_client.post(f"/api/connections/{conn_id}/my-credentials",
                             json={"auth_mode": "userpass", "credentials": {"username": "bob", "password": bob_pass}}, headers=owner_h)
    assert saved.status_code == 200, saved.text
    kms["mock"].REQUEST_LOG.clear()
    assert _file_ids(test_client.get(file_endpoint, headers=owner_h)) == _BOB_FILES
    assert _users_seen(kms) == {"bob"}, "once connected, every call runs as the owner's own login"


@pytest.mark.e2e
def test_admin_who_is_not_the_owner_runs_as_the_connection_identity(
    kms, test_client, create_user, login_user, whoami, create_data_source,
):
    """Admin branch of the A6 fallback: a second org admin, not the data-source owner and with
    no saved row, browses as the system credentials."""
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _user_required_source(kms, test_client, create_data_source, token, org, headers)
    _, admin_h, _ = _invite(test_client, create_user, login_user, org, headers, "admin2", role="admin")
    file_endpoint = f"/api/data_sources/{ds_id}/connections/{conn_id}/files"

    kms["mock"].REQUEST_LOG.clear()
    _assert_root_scoped(_file_ids(test_client.get(file_endpoint, headers=admin_h)))
    assert _users_seen(kms) == {"svc"}, "a second admin must run as the connection identity"


@pytest.mark.e2e
def test_member_credential_test_ignores_destination_keys(
    kms, test_client, create_user, login_user, whoami, create_data_source,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, _ = _user_required_source(kms, test_client, create_data_source, token, org, headers)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    bob_h = _member(test_client, create_user, login_user, org, headers, "bob")
    credentials = {"username": "bob", "password": kms["mock"].USERS["bob"]["password"],
                   "base_url": f"http://127.0.0.1:{closed_port}", "api_prefix": "/elsewhere", "allow_http": True}
    kms["mock"].REQUEST_LOG.clear()
    r = test_client.post(f"/api/data_sources/{ds_id}/my-credentials/test", json={"auth_mode": "userpass", "credentials": credentials},
                         headers=bob_h)
    assert r.status_code == 200 and r.json()["success"] is True, r.json()
    assert ("bob", "POST", _login(kms)) in kms["mock"].REQUEST_LOG, "bob must log in at the configured KMS under its prefix"
