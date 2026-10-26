"""Google Drive on a shared service account: members use it without signing in.

A Drive connection on the admin's OAuth client is per-user — a member who
hasn't connected their Google account gets "Connect required" (403). The
service-account variant is the shared alternative: one Google identity, so
any member with access to the agent reads the files shared with it, and
nobody signs in. Both paths go through the real routes, credential
resolution and client; only Google's HTTP API and token endpoint are faked.
"""
import uuid

import pytest

from tests.unit.test_google_drive_service_account import sample_drive, service_account_key


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def org(create_user, login_user, whoami, test_client):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]

    email = f"drive-member-{uuid.uuid4().hex[:8]}@example.com"
    invited = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email, "role": "member"},
        headers=_headers(admin_token, org_id),
    )
    assert invited.status_code == 200, invited.json()
    member = create_user(email=email)
    member_token = login_user(member["email"], member["password"])
    return org_id, admin_token, member_token


def _files_endpoint(test_client, create_domain_from_connection, conn_id, admin_token, org_id):
    ds = create_domain_from_connection(
        name=f"Drive {uuid.uuid4().hex[:6]}", connection_id=conn_id,
        user_token=admin_token, org_id=org_id, is_public=True,
    )
    return f"/api/data_sources/{ds['id']}/connections/{conn_id}/files"


@pytest.mark.e2e
def test_member_lists_service_account_files_without_signing_in(
    org, monkeypatch, test_client, create_connection, create_domain_from_connection,
):
    sample_drive().install(monkeypatch)
    org_id, admin_token, member_token = org

    conn = create_connection(
        name=f"Drive SA {uuid.uuid4().hex[:6]}", type="google_drive",
        config={"auth_type": "service_account"},
        credentials={"service_account_json": service_account_key()},
        auth_policy="system_only",
        user_token=admin_token, org_id=org_id,
    )
    endpoint = _files_endpoint(test_client, create_domain_from_connection, conn["id"], admin_token, org_id)

    for token in (admin_token, member_token):
        r = test_client.get(endpoint, params={"limit": 5000}, headers=_headers(token, org_id))
        assert r.status_code == 200, r.text
        assert sorted(f["path"] for f in r.json()["files"]) == [
            "Finance/budget.csv", "Team/report.csv", "direct.csv",
        ]

    # The connection reports a shared catalog, so the UI won't prompt sign-in.
    r = test_client.get(endpoint.rsplit("/", 2)[0], headers=_headers(admin_token, org_id))
    assert r.status_code == 200, r.text
    assert r.json()[0]["catalog_ownership"] == "shared", r.text


@pytest.mark.e2e
def test_member_on_the_oauth_client_variant_still_has_to_connect(
    org, monkeypatch, test_client, create_connection, create_domain_from_connection,
):
    sample_drive().install(monkeypatch)
    org_id, admin_token, member_token = org

    conn = create_connection(
        name=f"Drive OAuth {uuid.uuid4().hex[:6]}", type="google_drive",
        config={},
        credentials={"oauth_client_id": "cid", "oauth_client_secret": "secret"},
        auth_policy="user_required", allowed_user_auth_modes=["oauth"],
        user_token=admin_token, org_id=org_id,
    )
    endpoint = _files_endpoint(test_client, create_domain_from_connection, conn["id"], admin_token, org_id)

    r = test_client.get(endpoint, params={"limit": 5000}, headers=_headers(member_token, org_id))
    # The browser gets an empty listing flagged connect_required, not files.
    assert r.status_code == 200, r.text
    assert r.json()["connect_required"] is True
    assert r.json()["files"] == []
    # This connection stores no service identity, so the reason must not
    # send the user to a service account that doesn't exist.
    assert "service account" not in (r.json().get("reason") or "").lower()


@pytest.mark.e2e
def test_service_account_with_a_foreign_token_endpoint_is_refused(org, test_client):
    org_id, admin_token, _ = org
    r = test_client.post(
        "/api/connections",
        json={
            "name": f"Drive bad {uuid.uuid4().hex[:6]}", "type": "google_drive",
            "config": {"auth_type": "service_account"},
            "credentials": {"service_account_json": service_account_key(token_uri="https://attacker.example/token")},
            "auth_policy": "system_only",
        },
        headers=_headers(admin_token, org_id),
    )
    assert r.status_code == 400, r.text
