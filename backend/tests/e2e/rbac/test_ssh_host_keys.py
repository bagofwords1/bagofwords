"""Discovery authorizes before reaching the network and accepts no login secrets."""

import base64
from unittest.mock import patch

import pytest

from tests.e2e.rbac.test_remote_server import ssh_driver  # noqa: F401


@pytest.mark.usefixtures("ssh_driver")
def test_host_key_discovery_authorization_and_validation(
    test_client, create_user, login_user, whoami, invite_user_to_org, create_connection
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": "Bearer " + token, "X-Organization-Id": org}
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 32
    key = "ssh-ed25519 " + base64.b64encode(blob).decode()
    url = "/api/connections/ssh/host-keys"
    with patch(
        "app.services.ssh_host_keys.run_bounded", return_value=(0, ("server " + key + "\n").encode(), b"")
    ) as driver:
        r = test_client.post(url, headers=headers, json={"host": "127.0.0.1", "port": 22})
        assert r.status_code == 200, r.text
        assert r.json()["trusted"] is False
        assert r.json()["keys"][0]["public_key"] == key
        driver.reset_mock()
        assert test_client.post(url, json={"host": "127.0.0.1"}).status_code == 401
        assert (
            test_client.post(
                url, headers=headers, json={"host": "127.0.0.1", "password": "never-send-this"}
            ).status_code
            == 422
        )
        assert test_client.post(url, headers=headers, json={"host": "169.254.169.254"}).status_code == 400
        assert not driver.called
        member = invite_user_to_org(org_id=org, admin_token=token)
        member_headers = {"Authorization": "Bearer " + member["token"], "X-Organization-Id": org}
        r = test_client.post(url, headers=member_headers, json={"host": "127.0.0.1"})
        assert r.status_code == 403, r.text
        assert not driver.called

        connection = create_connection(
            name="SSH verification",
            type="remote_ssh",
            config={"host": "127.0.0.1", "host_public_key": key, "auth_type": "password"},
            credentials={"username": "reader", "password": "fixture"},
            user_token=token,
            org_id=org,
        )
        scoped = f"/api/connections/{connection['id']}/ssh/host-keys"
        assert test_client.post(scoped, headers=member_headers, json={"host": "127.0.0.1"}).status_code == 403
        assert not driver.called
        assert test_client.post(scoped, headers=headers, json={"host": "127.0.0.1"}).status_code == 200
