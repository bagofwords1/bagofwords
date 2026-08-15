"""End-to-end coverage for OAuth access to the main BOW application API."""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest

from app.settings.config import settings as bow_settings

APP_REDIRECT_URI = "http://localhost:4173/callback"


@pytest.fixture
def _allow_multiple_orgs():
    flags = bow_settings.bow_config.features
    saved = flags.allow_multiple_organizations
    flags.allow_multiple_organizations = True
    try:
        yield
    finally:
        flags.allow_multiple_organizations = saved


def _pkce_pair():
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _issue_token(test_client, *, login_token, client, scope):
    verifier, challenge = _pkce_pair()
    authorize = test_client.post(
        "/api/oauth/authorize",
        json={
            "client_id": client["client_id"],
            "redirect_uri": APP_REDIRECT_URI,
            "state": "state with reserved & characters",
            "scope": scope,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        headers={"Authorization": f"Bearer {login_token}"},
    )
    assert authorize.status_code == 200, authorize.json()
    callback = urlparse(authorize.json()["redirect_url"])
    callback_params = parse_qs(callback.query)
    assert callback_params["state"] == ["state with reserved & characters"]

    token_response = test_client.post(
        "/api/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": callback_params["code"][0],
            "redirect_uri": APP_REDIRECT_URI,
            "client_id": client["client_id"],
            "code_verifier": verifier,
        },
    )
    assert token_response.status_code == 200, token_response.json()
    return token_response.json()


@pytest.mark.e2e
def test_app_protected_resource_metadata(test_client):
    response = test_client.get("/.well-known/oauth-protected-resource/api")
    assert response.status_code == 200
    data = response.json()
    assert data["resource"].endswith("/api")
    assert data["scopes_supported"] == ["app"]

    auth_server = test_client.get("/.well-known/oauth-authorization-server").json()
    assert set(auth_server["scopes_supported"]) == {"app", "mcp"}


@pytest.mark.e2e
def test_create_and_inspect_trusted_app_client(
    create_oauth_client,
    list_oauth_clients,
    test_client,
    create_user,
    login_user,
    whoami,
):
    user = create_user()
    login_token = login_user(user["email"], user["password"])
    org_id = whoami(login_token)["organizations"][0]["id"]

    client = create_oauth_client(
        user_token=login_token,
        org_id=org_id,
        name="Acme Insights",
        redirect_uris=[APP_REDIRECT_URI],
        scopes="app",
        trusted=True,
    )
    assert client["scopes"] == ["app"]
    assert client["trusted"] is True

    listed = list_oauth_clients(user_token=login_token, org_id=org_id)
    saved = next(item for item in listed if item["id"] == client["id"])
    assert saved["scopes"] == ["app"]
    assert saved["trusted"] is True
    assert saved["active_token_count"] == 0
    assert saved["last_used_at"] is None

    info = test_client.get(
        f"/api/oauth/clients/{client['client_id']}/info",
        params={"redirect_uri": APP_REDIRECT_URI, "scope": "app"},
    )
    assert info.status_code == 200
    assert info.json() == {
        "client_id": client["client_id"],
        "name": "Acme Insights",
        "scopes": ["app"],
        "requested_scopes": ["app"],
        "trusted": True,
    }


@pytest.mark.e2e
def test_member_without_manage_settings_cannot_manage_oauth_clients(
    test_client,
    create_user,
    login_user,
    whoami,
):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    member_email = f"oauth-settings-member-{secrets.token_hex(6)}@example.com"
    invite = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers={
            "Authorization": f"Bearer {admin_token}",
            "X-Organization-Id": org_id,
        },
    )
    assert invite.status_code == 200, invite.json()
    member = create_user(email=member_email)
    member_token = login_user(member["email"], member["password"])
    member_headers = {
        "Authorization": f"Bearer {member_token}",
        "X-Organization-Id": org_id,
    }

    listed = test_client.get("/api/oauth/clients", headers=member_headers)
    assert listed.status_code == 403

    created = test_client.post(
        "/api/oauth/clients",
        json={
            "name": "Unauthorized client",
            "redirect_uris": [APP_REDIRECT_URI],
            "scopes": "app",
        },
        headers=member_headers,
    )
    assert created.status_code == 403


@pytest.mark.e2e
def test_app_token_is_org_pinned_and_cannot_access_mcp(
    _allow_multiple_orgs,
    create_oauth_client,
    create_organization,
    create_report,
    list_oauth_clients,
    test_client,
    create_user,
    login_user,
    whoami,
):
    user = create_user()
    login_token = login_user(user["email"], user["password"])
    org_a = whoami(login_token)["organizations"][0]["id"]
    org_b = create_organization(name="Other tenant", user_token=login_token)
    report_a = create_report(
        title="Pinned report",
        user_token=login_token,
        org_id=org_a,
    )
    create_report(
        title="Wrong tenant report",
        user_token=login_token,
        org_id=org_b,
    )
    client = create_oauth_client(
        user_token=login_token,
        org_id=org_a,
        redirect_uris=[APP_REDIRECT_URI],
        scopes="app",
        trusted=True,
    )

    token = _issue_token(
        test_client,
        login_token=login_token,
        client=client,
        scope="app",
    )
    assert 3600 <= token["expires_in"] <= 24 * 60 * 60

    reports = test_client.get(
        "/api/reports?filter=my&limit=20",
        headers={
            "Authorization": f"Bearer {token['access_token']}",
            "X-Organization-Id": org_b,
        },
    )
    assert reports.status_code == 200, reports.json()
    report_ids = {report["id"] for report in reports.json()["reports"]}
    assert report_a["id"] in report_ids
    assert all(report["title"] != "Wrong tenant report" for report in reports.json()["reports"])

    listed = list_oauth_clients(user_token=login_token, org_id=org_a)
    saved = next(item for item in listed if item["id"] == client["id"])
    assert saved["active_token_count"] == 1
    assert saved["last_used_at"] is not None

    mcp = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {token['access_token']}"},
    )
    assert mcp.status_code == 401


@pytest.mark.e2e
def test_mcp_token_cannot_access_app_api(
    create_oauth_client,
    test_client,
    create_user,
    login_user,
    whoami,
):
    user = create_user()
    login_token = login_user(user["email"], user["password"])
    org_id = whoami(login_token)["organizations"][0]["id"]
    client = create_oauth_client(
        user_token=login_token,
        org_id=org_id,
        redirect_uris=[APP_REDIRECT_URI],
        scopes="mcp",
    )
    token = _issue_token(
        test_client,
        login_token=login_token,
        client=client,
        scope="mcp",
    )

    response = test_client.get(
        "/api/reports?filter=my&limit=1",
        headers={"Authorization": f"Bearer {token['access_token']}"},
    )
    assert response.status_code == 401


@pytest.mark.e2e
def test_deleting_client_revokes_app_tokens(
    create_oauth_client,
    test_client,
    create_user,
    login_user,
    whoami,
):
    user = create_user()
    login_token = login_user(user["email"], user["password"])
    org_id = whoami(login_token)["organizations"][0]["id"]
    client = create_oauth_client(
        user_token=login_token,
        org_id=org_id,
        redirect_uris=[APP_REDIRECT_URI],
        scopes="app",
    )
    token = _issue_token(
        test_client,
        login_token=login_token,
        client=client,
        scope="app",
    )

    deleted = test_client.delete(
        f"/api/oauth/clients/{client['id']}",
        headers={
            "Authorization": f"Bearer {login_token}",
            "X-Organization-Id": org_id,
        },
    )
    assert deleted.status_code == 200

    response = test_client.get(
        "/api/reports?filter=my&limit=1",
        headers={"Authorization": f"Bearer {token['access_token']}"},
    )
    assert response.status_code == 401


@pytest.mark.e2e
def test_app_refresh_rotates_both_credentials_and_preserves_scope(
    create_oauth_client,
    test_client,
    create_user,
    login_user,
    whoami,
):
    user = create_user()
    login_token = login_user(user["email"], user["password"])
    org_id = whoami(login_token)["organizations"][0]["id"]
    client = create_oauth_client(
        user_token=login_token,
        org_id=org_id,
        redirect_uris=[APP_REDIRECT_URI],
        scopes="app",
    )
    original = _issue_token(
        test_client,
        login_token=login_token,
        client=client,
        scope="app",
    )

    refreshed_response = test_client.post(
        "/api/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": original["refresh_token"],
            "client_id": client["client_id"],
        },
    )
    assert refreshed_response.status_code == 200, refreshed_response.json()
    refreshed = refreshed_response.json()
    assert refreshed["scope"] == "app"
    assert 3600 <= refreshed["expires_in"] <= 24 * 60 * 60
    assert refreshed["access_token"] != original["access_token"]
    assert refreshed["refresh_token"] != original["refresh_token"]

    old_access = test_client.get(
        "/api/reports?filter=my&limit=1",
        headers={"Authorization": f"Bearer {original['access_token']}"},
    )
    assert old_access.status_code == 401

    reused_refresh = test_client.post(
        "/api/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": original["refresh_token"],
            "client_id": client["client_id"],
        },
    )
    assert reused_refresh.status_code == 400

    current_access = test_client.get(
        "/api/reports?filter=my&limit=1",
        headers={"Authorization": f"Bearer {refreshed['access_token']}"},
    )
    assert current_access.status_code == 200, current_access.json()


@pytest.mark.e2e
def test_changing_client_scopes_revokes_existing_access_and_refresh_tokens(
    create_oauth_client,
    test_client,
    create_user,
    login_user,
    whoami,
):
    user = create_user()
    login_token = login_user(user["email"], user["password"])
    org_id = whoami(login_token)["organizations"][0]["id"]
    client = create_oauth_client(
        user_token=login_token,
        org_id=org_id,
        redirect_uris=[APP_REDIRECT_URI],
        scopes="app mcp",
    )
    token = _issue_token(
        test_client,
        login_token=login_token,
        client=client,
        scope="app",
    )

    changed = test_client.patch(
        f"/api/oauth/clients/{client['id']}",
        json={"scopes": "mcp"},
        headers={
            "Authorization": f"Bearer {login_token}",
            "X-Organization-Id": org_id,
        },
    )
    assert changed.status_code == 200, changed.json()
    assert changed.json()["scopes"] == ["mcp"]

    access = test_client.get(
        "/api/reports?filter=my&limit=1",
        headers={"Authorization": f"Bearer {token['access_token']}"},
    )
    assert access.status_code == 401

    refresh = test_client.post(
        "/api/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id": client["client_id"],
        },
    )
    assert refresh.status_code == 400


@pytest.mark.e2e
def test_removed_member_cannot_keep_using_an_app_token(
    create_oauth_client,
    test_client,
    create_user,
    login_user,
    whoami,
):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]

    # Invitation is the public way to produce a second live membership in the
    # first user's organization; create_user follows the real invite link data.
    member_email = f"oauth-member-{secrets.token_hex(6)}@example.com"
    invite = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers={
            "Authorization": f"Bearer {admin_token}",
            "X-Organization-Id": org_id,
        },
    )
    assert invite.status_code == 200, invite.json()
    membership_id = invite.json()["id"]

    member = create_user(email=member_email)
    member_token = login_user(member["email"], member["password"])
    client = create_oauth_client(
        user_token=admin_token,
        org_id=org_id,
        redirect_uris=[APP_REDIRECT_URI],
        scopes="app",
    )
    oauth_token = _issue_token(
        test_client,
        login_token=member_token,
        client=client,
        scope="app",
    )

    before_removal = test_client.get(
        "/api/reports?filter=my&limit=1",
        headers={"Authorization": f"Bearer {oauth_token['access_token']}"},
    )
    assert before_removal.status_code == 200, before_removal.json()

    removed = test_client.delete(
        f"/api/organizations/{org_id}/members/{membership_id}",
        headers={
            "Authorization": f"Bearer {admin_token}",
            "X-Organization-Id": org_id,
        },
    )
    assert removed.status_code == 204

    after_removal = test_client.get(
        "/api/reports?filter=my&limit=1",
        headers={"Authorization": f"Bearer {oauth_token['access_token']}"},
    )
    assert after_removal.status_code == 401

    refresh_after_removal = test_client.post(
        "/api/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": oauth_token["refresh_token"],
            "client_id": client["client_id"],
        },
    )
    assert refresh_after_removal.status_code == 400
