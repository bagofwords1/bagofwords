"""Real connection API/DB flow; only the SharePoint HTTP server is simulated."""
import pytest
import uuid
from tests.unit.test_sharepoint_onprem_client import remote, SITE


@pytest.mark.e2e
def test_onprem_connection_lifecycle_and_member_boundary(
    remote, test_client, create_user, login_user, whoami,
    create_connection, get_connection, test_connection_connectivity,
    refresh_connection_schema, get_connection_tables, add_organization_member,
):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org = whoami(token)["organizations"][0]["id"]
    secret = "unique-ntlm-secret-never-returned"
    conn = create_connection(name="SharePoint Server QA", type="sharepoint_onprem",
        config={"site_url": SITE, "recursive": True},
        credentials={"username": "DOMAIN\\reader", "password": secret},
        user_token=token, org_id=org)
    assert conn["type"] == "sharepoint_onprem"
    details = get_connection(connection_id=conn["id"], user_token=token, org_id=org)
    assert secret not in str(details)
    probe = test_connection_connectivity(connection_id=conn["id"], user_token=token, org_id=org)
    assert probe["success"] is True
    refresh_connection_schema(connection_id=conn["id"], user_token=token, org_id=org)
    tables = get_connection_tables(connection_id=conn["id"], user_token=token, org_id=org)
    assert len(tables) == len(remote["blobs"])

    email = f"sharepoint-member-{uuid.uuid4().hex[:8]}@example.com"
    invited = test_client.post(f"/api/organizations/{org}/members", json={"organization_id": org, "email": email, "role": "member"},
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org})
    assert invited.status_code == 200, invited.json()
    member = create_user(email=email)
    member_token = login_user(member["email"], member["password"])
    headers = {"Authorization": f"Bearer {member_token}", "X-Organization-Id": org}
    changed = test_client.put(f"/api/connections/{conn['id']}", json={"name": "Unauthorized change"}, headers=headers)
    assert changed.status_code == 403
    assert secret not in changed.text


@pytest.mark.e2e
def test_onprem_credentials_are_required_and_bad_auth_cannot_pass(
    remote, test_client, create_user, login_user, whoami,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org}
    r = test_client.post("/api/connections", json={"name": "No credentials", "type": "sharepoint_onprem",
        "config": {"site_url": SITE}, "credentials": {}, "auth_policy": "system_only"}, headers=headers)
    assert r.status_code in (400, 422)
    remote["status"] = 401
    r = test_client.post("/api/connections/test-params", json={"name": "Bad credentials", "type": "sharepoint_onprem",
        "config": {"site_url": SITE}, "credentials": {"username": "DOMAIN\\wrong", "password": "incorrect"}}, headers=headers)
    assert r.status_code == 200 and r.json()["success"] is False, r.json()
    assert "incorrect" not in r.text


@pytest.mark.e2e
def test_blank_library_scope_is_identical_before_and_after_save(
    remote, test_client, create_user, login_user, whoami, create_connection, get_connection_tables, refresh_connection_schema,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org}
    payload = {"name": "Default library only", "type": "sharepoint_onprem",
        "config": {"site_url": SITE, "drive_name": "", "recursive": True},
        "credentials": {"username": "DOMAIN\\reader", "password": "test-only"}}
    r = test_client.post("/api/connections/test-params", json=payload, headers=headers)
    assert r.status_code == 200 and r.json()["success"], r.json()
    expected = sum("/Shared Documents/" in p for p in remote["blobs"])
    assert r.json()["table_count"] == expected
    conn = create_connection(**payload, user_token=token, org_id=org)
    refresh_connection_schema(connection_id=conn["id"], user_token=token, org_id=org)
    assert len(get_connection_tables(connection_id=conn["id"], user_token=token, org_id=org)) == expected


@pytest.mark.e2e
def test_user_required_connection_uses_member_credentials_not_system_identity(
    remote, test_client, create_user, login_user, whoami, create_data_source,
):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org}
    ds = create_data_source(name="Windows user scoped files", type="sharepoint_onprem",
        config={"site_url": SITE}, credentials={"username": "DOMAIN\\service", "password": "system-secret"},
        auth_policy="user_required", user_token=token, org_id=org)
    ds_id = ds["id"]
    r = test_client.put(f"/api/data_sources/{ds_id}", json={"is_public": True}, headers=headers)
    assert r.status_code == 200, r.json()
    email = f"sp-reader-{uuid.uuid4().hex[:8]}@example.com"
    r = test_client.post(f"/api/organizations/{org}/members", json={"organization_id": org, "email": email, "role": "member"}, headers=headers)
    assert r.status_code == 200, r.json()
    member = create_user(email=email)
    mt = login_user(member["email"], member["password"])
    mh = {"Authorization": f"Bearer {mt}", "X-Organization-Id": org}
    connections = test_client.get(f"/api/data_sources/{ds_id}/connections", headers=headers).json()
    endpoint = f"/api/connections/{connections[0]['id']}/my-credentials"
    visible = test_client.get(f"/api/data_sources/{ds_id}/file-connections", headers=mh)
    assert visible.status_code == 200, visible.json()
    assert [c["id"] for c in visible.json()] == [connections[0]["id"]]
    assert "credentials" not in visible.text and "system-secret" not in visible.text
    assert test_client.get(f"/api/data_sources/{ds_id}/connections", headers=mh).status_code == 403
    file_endpoint = f"/api/data_sources/{ds_id}/connections/{connections[0]['id']}/files"
    remote["identities"].clear()
    r = test_client.get(file_endpoint, headers=mh)
    assert r.status_code == 403 or r.json().get("connect_required") is True, r.json()
    assert not remote["identities"]
    r = test_client.post(endpoint, json={"auth_mode": "kerberos", "credentials": {}}, headers=mh)
    assert r.status_code == 400
    r = test_client.post(endpoint, json={"auth_mode": "ntlm", "credentials": {}}, headers=mh)
    assert r.status_code == 422
    r = test_client.post(endpoint, json={"auth_mode": "ntlm", "credentials": {"username": "DOMAIN\\member", "password": "member-secret"}}, headers=mh)
    assert r.status_code == 200, r.json()
    assert "member-secret" not in r.text
    remote["identities"].clear()
    r = test_client.get(file_endpoint, headers=mh)
    assert r.status_code == 200 and r.json()["files"], r.json()
    assert remote["identities"] and set(remote["identities"]) == {"DOMAIN\\member"}
    r = test_client.delete(endpoint, headers=mh)
    assert r.status_code == 200
    remote["identities"].clear()
    r = test_client.get(file_endpoint, headers=mh)
    assert r.status_code == 403 or r.json().get("connect_required") is True, r.json()
    assert not remote["identities"]
