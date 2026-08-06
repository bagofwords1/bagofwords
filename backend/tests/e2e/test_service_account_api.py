"""Service-account keys as a first-class API credential.

A service account is the credential an integration (embedded chat, a backend
proxy, a scheduled job) authenticates with instead of a human's personal API
key. These tests lock the contract that surface depends on: the key
authenticates like any other principal, the account can read its own profile,
and it cannot escalate into identity management.
"""
import uuid

import pytest


def _create_service_account(test_client, user_token, org_id, name=None):
    name = name or f"sa-{uuid.uuid4().hex[:8]}"
    headers = {"Authorization": f"Bearer {user_token}", "X-Organization-Id": str(org_id)}
    r = test_client.post("/api/service_accounts", json={"name": name}, headers=headers)
    assert r.status_code == 200, r.json()
    sa = r.json()
    r = test_client.post(
        f"/api/service_accounts/{sa['id']}/keys",
        json={"name": "integration"},
        headers=headers,
    )
    assert r.status_code == 200, r.json()
    return sa, r.json()["key"]


@pytest.mark.e2e
def test_service_account_key_authenticates_and_reads_own_profile(
    test_client, create_user, login_user, whoami
):
    """Any authenticated principal — human or service account — can read its own
    profile from /users/whoami.

    Regression: service accounts carry a synthetic ``@service.invalid`` address,
    which the profile schema's EmailStr validation rejected, 500-ing the very
    call an integration makes to verify its key.
    """
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    sa, key = _create_service_account(test_client, token, org_id)

    for headers in (
        {"X-API-Key": key},
        {"Authorization": f"Bearer {key}"},  # both documented forms
    ):
        r = test_client.get("/api/users/whoami", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == sa["name"]
        assert body["email"]
        # ``organizations`` reflects seat-consuming memberships; a service
        # account is bound to its org through the service_accounts row instead,
        # so the list is legitimately empty. Assert only that the field is
        # well-formed — the org binding itself is covered by the report test
        # below, which never sends X-Organization-Id.
        assert isinstance(body["organizations"], list)


@pytest.mark.e2e
def test_service_account_key_can_drive_the_report_api(
    test_client, create_user, login_user, whoami
):
    """A service-account key carries its account's org role, so it can create
    and read the resources an integration needs (reports)."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    _sa, key = _create_service_account(test_client, token, org_id)

    headers = {"X-API-Key": key}
    title = f"integration-{uuid.uuid4().hex[:8]}"
    r = test_client.post("/api/reports", json={"title": title, "data_sources": []}, headers=headers)
    assert r.status_code == 200, r.text
    report_id = r.json()["id"]

    r = test_client.get(f"/api/reports/{report_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["title"] == title


@pytest.mark.e2e
def test_service_account_cannot_manage_identities(
    test_client, create_user, login_user, whoami
):
    """A leaked integration key must not be able to mint more credentials."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    _sa, key = _create_service_account(test_client, token, org_id)

    headers = {"X-API-Key": key, "X-Organization-Id": str(org_id)}
    assert test_client.post("/api/api_keys", json={"name": "x"}, headers=headers).status_code == 403
    assert test_client.get("/api/service_accounts", headers=headers).status_code == 403
    assert test_client.post(
        "/api/service_accounts", json={"name": "clone"}, headers=headers
    ).status_code == 403
