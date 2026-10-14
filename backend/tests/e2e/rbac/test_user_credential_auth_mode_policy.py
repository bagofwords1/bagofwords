"""
A connection's `allowed_user_auth_modes` is the admin's policy for HOW members
may authenticate with their own credentials. Every per-user credential path on
/api/data_sources/{id}/my-credentials must honour it — not just POST:

  - POST   /my-credentials          (already enforced; regression guard)
  - PATCH  /my-credentials          (switching auth_mode must not side-step it)
  - POST   /my-credentials/test     (must reject BEFORE any outbound connect)

World: a Snowflake connection (registry user-scope modes: userpass, keypair,
oauth) set to `user_required` with only `userpass` allowed, one agent on it,
and a member with `view` on the agent. The outbound client is replaced by a
recorder so the test can prove no connection attempt happens for a
disallowed mode.
"""
import pytest
from unittest.mock import patch

from tests.fixtures.connection_tool import _skip_network


ALLOWED = "userpass"
USERPASS = {"user": "member_user", "password": "pw"}
KEYPAIR = {"user": "member_user", "private_key_pem": "-----BEGIN PRIVATE KEY-----\nAAA\n-----END PRIVATE KEY-----"}


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


class _RecordingClient:
    calls: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def atest_connection(self):
        _RecordingClient.calls.append(self.kwargs)
        return {"success": True, "message": "ok"}


@pytest.fixture
def outbound():
    _RecordingClient.calls = []
    with patch(
        "app.services.user_data_source_credentials_service.resolve_client_class",
        lambda _t: _RecordingClient,
    ):
        yield _RecordingClient.calls


def _world(test_client, bootstrap_admin, invite_user_to_org, create_connection, grant_resource, allowed):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    ah = _hdr(admin["token"], org_id)
    with _skip_network():
        conn = create_connection(
            name="sf-user-required", type="snowflake",
            config={"account": "acme", "warehouse": "wh", "database": "db"},
            credentials={}, auth_policy="user_required",
            allowed_user_auth_modes=allowed,
            user_token=admin["token"], org_id=org_id,
        )
        r = test_client.post("/api/data_sources", headers=ah, json={
            "name": "sf-agent", "connection_ids": [conn["id"]],
            "generate_summary": False, "generate_conversation_starters": False,
            "generate_ai_rules": False,
        })
    assert r.status_code == 200, r.text
    ds_id = r.json()["id"]
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    g = grant_resource(resource_type="data_source", resource_id=ds_id,
                       principal_type="user", principal_id=member["user_id"],
                       permissions=["view"], user_token=admin["token"], org_id=org_id)
    assert g.status_code == 200, g.json()
    return {"org_id": org_id, "ds_id": ds_id, "conn_id": conn["id"],
            "admin": _hdr(admin["token"], org_id), "member": _hdr(member["token"], org_id)}


@pytest.fixture
def world(test_client, bootstrap_admin, invite_user_to_org, create_connection, grant_resource):
    return _world(test_client, bootstrap_admin, invite_user_to_org, create_connection,
                  grant_resource, [ALLOWED])


def _url(w, suffix=""):
    return f"/api/data_sources/{w['ds_id']}/my-credentials{suffix}"


@pytest.mark.e2e
@pytest.mark.parametrize("role", ["member", "admin"])
@pytest.mark.parametrize("mode,creds", [("keypair", KEYPAIR)])
def test_patch_cannot_switch_to_disallowed_auth_mode(test_client, world, role, mode, creds):
    w = world
    h = w[role]
    r = test_client.post(_url(w), headers=h, json={"auth_mode": ALLOWED, "credentials": USERPASS})
    assert r.status_code == 200, r.text

    r = test_client.patch(_url(w), headers=h, json={"auth_mode": mode, "credentials": creds})
    assert r.status_code == 400, r.text

    stored = test_client.get(_url(w), headers=h)
    assert stored.status_code == 200
    assert stored.json()["auth_mode"] == ALLOWED


@pytest.mark.e2e
@pytest.mark.parametrize("role", ["member", "admin"])
@pytest.mark.parametrize("mode,creds", [("keypair", KEYPAIR), ("oauth", {})])
def test_credential_test_rejects_disallowed_mode_before_connecting(test_client, world, outbound, role, mode, creds):
    w = world
    r = test_client.post(_url(w, "/test"), headers=w[role], json={"auth_mode": mode, "credentials": creds})
    assert r.status_code == 400, r.text
    assert outbound == [], "no outbound connection may be attempted for a disallowed auth mode"


@pytest.mark.e2e
def test_post_still_rejects_disallowed_mode(test_client, world):
    w = world
    r = test_client.post(_url(w), headers=w["member"], json={"auth_mode": "keypair", "credentials": KEYPAIR})
    assert r.status_code == 400, r.text


# ── must keep working ──────────────────────────────────────────────────


@pytest.mark.e2e
def test_allowed_mode_save_test_and_patch_still_work(test_client, world, outbound):
    w = world
    h = w["member"]
    r = test_client.post(_url(w, "/test"), headers=h, json={"auth_mode": ALLOWED, "credentials": USERPASS})
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True
    assert len(outbound) == 1

    r = test_client.post(_url(w), headers=h, json={"auth_mode": ALLOWED, "credentials": USERPASS})
    assert r.status_code == 200, r.text

    # Rotating the secret in the existing (allowed) mode, with or without
    # restating the mode, keeps working.
    r = test_client.patch(_url(w), headers=h, json={"credentials": {"user": "member_user", "password": "pw2"}})
    assert r.status_code == 200, r.text
    r = test_client.patch(_url(w), headers=h, json={"auth_mode": ALLOWED, "credentials": {"user": "member_user", "password": "pw3"}})
    assert r.status_code == 200, r.text
    assert r.json()["auth_mode"] == ALLOWED

    # Non-credential edits on an existing row are untouched.
    r = test_client.patch(_url(w), headers=h, json={"is_primary": True})
    assert r.status_code == 200, r.text


@pytest.mark.e2e
def test_switching_between_modes_the_policy_allows(test_client, bootstrap_admin, invite_user_to_org,
                                                   create_connection, grant_resource, outbound):
    w = _world(test_client, bootstrap_admin, invite_user_to_org, create_connection,
               grant_resource, ["userpass", "keypair"])
    h = w["member"]
    assert test_client.post(_url(w), headers=h, json={"auth_mode": "userpass", "credentials": USERPASS}).status_code == 200
    r = test_client.patch(_url(w), headers=h, json={"auth_mode": "keypair", "credentials": KEYPAIR})
    assert r.status_code == 200, r.text
    assert r.json()["auth_mode"] == "keypair"
    r = test_client.post(_url(w, "/test"), headers=h, json={"auth_mode": "keypair", "credentials": KEYPAIR})
    assert r.status_code == 200, r.text
    assert len(outbound) == 1


@pytest.mark.e2e
def test_oauth_mode_allowed_by_policy_is_accepted(test_client, bootstrap_admin, invite_user_to_org,
                                                  create_connection, grant_resource, outbound):
    """OAuth-only connections: the policy lists `oauth`, so saving/testing in
    that mode must not be rejected by the auth-mode check."""
    w = _world(test_client, bootstrap_admin, invite_user_to_org, create_connection,
               grant_resource, ["oauth"])
    h = w["member"]
    r = test_client.post(_url(w, "/test"), headers=h, json={"auth_mode": "oauth", "credentials": {}})
    assert r.status_code == 200, r.text
    r = test_client.post(_url(w), headers=h, json={"auth_mode": "oauth", "credentials": {}})
    assert r.status_code == 200, r.text
    # And the other modes stay closed on that connection.
    r = test_client.post(_url(w, "/test"), headers=h, json={"auth_mode": "userpass", "credentials": USERPASS})
    assert r.status_code == 400, r.text
