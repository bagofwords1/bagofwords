import pytest
import uuid


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}


@pytest.mark.e2e
def test_default_member_role_defaults_to_member(test_client, create_user, login_user, whoami):
    """With nothing configured, the org-wide default resolves to "member"."""
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    resp = test_client.get("/api/organization/default-member-role", headers=_auth(token, org_id))
    assert resp.status_code == 200, resp.json()
    assert resp.json()["role"] == "member"


@pytest.mark.e2e
def test_set_and_read_default_member_role(test_client, create_user, login_user, whoami):
    """A valid role can be persisted and is read back; the response echoes it."""
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    put = test_client.put(
        "/api/organization/default-member-role",
        json={"role": "admin"},
        headers=_auth(token, org_id),
    )
    assert put.status_code == 200, put.json()
    assert put.json()["role"] == "admin"

    get = test_client.get("/api/organization/default-member-role", headers=_auth(token, org_id))
    assert get.status_code == 200
    assert get.json()["role"] == "admin"


@pytest.mark.e2e
def test_default_member_role_rejects_unknown_role(test_client, create_user, login_user, whoami):
    """A role that is neither a system role nor owned by the org is rejected."""
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    resp = test_client.put(
        "/api/organization/default-member-role",
        json={"role": f"nope_{uuid.uuid4().hex[:6]}"},
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 400, resp.json()


@pytest.mark.e2e
def test_invite_without_role_uses_configured_default(test_client, create_user, login_user, whoami):
    """Omitting role on an invite applies the org-wide default, not a hard-coded
    "member"."""
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    # Configure a non-member default.
    put = test_client.put(
        "/api/organization/default-member-role",
        json={"role": "admin"},
        headers=_auth(token, org_id),
    )
    assert put.status_code == 200, put.json()

    # Invite WITHOUT specifying a role.
    email = f"invitee_{uuid.uuid4().hex[:8]}@test.com"
    invite = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email},
        headers=_auth(token, org_id),
    )
    assert invite.status_code == 200, invite.json()
    assert invite.json()["role"] == "admin"


@pytest.mark.e2e
def test_invite_with_explicit_role_overrides_default(test_client, create_user, login_user, whoami):
    """An explicit role on the invite wins over the configured default."""
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    put = test_client.put(
        "/api/organization/default-member-role",
        json={"role": "admin"},
        headers=_auth(token, org_id),
    )
    assert put.status_code == 200, put.json()

    email = f"invitee_{uuid.uuid4().hex[:8]}@test.com"
    invite = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email, "role": "member"},
        headers=_auth(token, org_id),
    )
    assert invite.status_code == 200, invite.json()
    assert invite.json()["role"] == "member"
