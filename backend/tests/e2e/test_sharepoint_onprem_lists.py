"""SharePoint Server lists through the real connection API and DB.

Only the SharePoint HTTP server is simulated (tests/fixtures/fake_sharepoint_lists.py).
"""
import uuid

import pytest

from tests.e2e.test_sharepoint_onprem import _wait_indexed
from tests.unit.test_sharepoint_onprem_lists_client import SITE, sp_lists  # noqa: F401  (fixture)

VISIBLE_LISTS = {"Expenses", "Tickets", "Projects", "Rota", "Salaries"}


def _admin(create_user, login_user, whoami):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org = whoami(token)["organizations"][0]["id"]
    return token, org, {"Authorization": f"Bearer {token}", "X-Organization-Id": org}


@pytest.mark.e2e
def test_lists_connection_lifecycle_catalogs_every_list_as_a_table(
    sp_lists, test_client, create_user, login_user, whoami, create_connection, get_connection,
    test_connection_connectivity, refresh_connection_schema, get_connection_tables,
):
    token, org, _ = _admin(create_user, login_user, whoami)
    secret = "unique-ntlm-secret-never-returned"
    conn = create_connection(name="Ops lists", type="sharepoint_onprem_lists",
        config={"site_url": SITE}, credentials={"username": "CORP\\svc-sharepoint", "password": secret},
        user_token=token, org_id=org)
    assert conn["type"] == "sharepoint_onprem_lists"
    assert secret not in str(get_connection(connection_id=conn["id"], user_token=token, org_id=org))

    probe = test_connection_connectivity(connection_id=conn["id"], user_token=token, org_id=org)
    assert probe["success"] is True, probe

    refresh_connection_schema(connection_id=conn["id"], user_token=token, org_id=org)
    _wait_indexed(test_client, conn["id"], token, org)
    tables = get_connection_tables(connection_id=conn["id"], user_token=token, org_id=org)
    assert {t["name"] for t in tables} == VISIBLE_LISTS
    expenses = next(t for t in tables if t["name"] == "Expenses")
    assert expenses["column_count"] >= 15
    # Cataloguing reads list metadata only; it never pages through items.
    assert not [r for r in sp_lists.requests if r["path"].endswith("/items")]


@pytest.mark.e2e
def test_lists_scope_and_bad_credentials_through_test_params(
    sp_lists, test_client, create_user, login_user, whoami,
):
    _, _, headers = _admin(create_user, login_user, whoami)
    payload = {"name": "Scoped", "type": "sharepoint_onprem_lists",
               "config": {"site_url": SITE, "lists": "Expenses, Projects"},
               "credentials": {"username": "CORP\\svc-sharepoint", "password": "test-only"}}
    r = test_client.post("/api/connections/test-params", json=payload, headers=headers)
    assert r.status_code == 200 and r.json()["success"], r.json()
    assert r.json()["table_count"] == 2

    sp_lists.fail_next.append((401, {"error": {"code": "x", "message": {"value": "dc01.corp internal"}}}))
    payload["credentials"] = {"username": "CORP\\wrong", "password": "incorrect-password"}
    r = test_client.post("/api/connections/test-params", json=payload, headers=headers)
    assert r.status_code == 200 and r.json()["success"] is False, r.json()
    assert "incorrect-password" not in r.text and "dc01.corp" not in r.text

    r = test_client.post("/api/connections", json={**payload, "credentials": {}, "auth_policy": "system_only"}, headers=headers)
    assert r.status_code in (400, 422)
    r = test_client.post("/api/connections/test-params", json={**payload, "config": {"site_url": "http://plain.example/sites/x"},
                                                              "credentials": {"username": "u", "password": "p"}}, headers=headers)
    assert r.status_code in (200, 400, 422)
    assert r.status_code != 200 or r.json()["success"] is False


@pytest.mark.e2e
def test_user_required_lists_use_each_members_own_windows_login(
    sp_lists, test_client, create_user, login_user, whoami, create_data_source,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    ds = create_data_source(name="Per-user lists", type="sharepoint_onprem_lists",
        config={"site_url": SITE}, credentials={"username": "CORP\\svc-sharepoint", "password": "system-secret"},
        auth_policy="user_required", user_token=token, org_id=org)
    r = test_client.put(f"/api/data_sources/{ds['id']}", json={"is_public": True}, headers=headers)
    assert r.status_code == 200, r.json()
    connection_id = test_client.get(f"/api/data_sources/{ds['id']}/connections", headers=headers).json()[0]["id"]

    email = f"lists-member-{uuid.uuid4().hex[:8]}@example.com"
    r = test_client.post(f"/api/organizations/{org}/members",
                         json={"organization_id": org, "email": email, "role": "member"}, headers=headers)
    assert r.status_code == 200, r.json()
    member = create_user(email=email)
    mh = {"Authorization": f"Bearer {login_user(member['email'], member['password'])}", "X-Organization-Id": org}
    endpoint = f"/api/connections/{connection_id}/my-credentials"

    # Kerberos is a service-account mode; a member's login must be complete.
    assert test_client.post(endpoint, json={"auth_mode": "kerberos", "credentials": {}}, headers=mh).status_code == 400
    assert test_client.post(endpoint, json={"auth_mode": "ntlm", "credentials": {}}, headers=mh).status_code == 422

    r = test_client.post(endpoint, json={"auth_mode": "ntlm",
                                         "credentials": {"username": "CORP\\noa", "password": "member-secret"}}, headers=mh)
    assert r.status_code == 200, r.json()
    assert "member-secret" not in r.text

    # A member cannot change the connection or read its system credentials.
    changed = test_client.put(f"/api/connections/{connection_id}", json={"name": "x"}, headers=mh)
    assert changed.status_code == 403 and "system-secret" not in changed.text
    assert test_client.delete(endpoint, headers=mh).status_code == 200
