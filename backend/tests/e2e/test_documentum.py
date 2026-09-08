"""Real connection API/DB flow for the Documentum connector; only the
Documentum REST/OTDS server is simulated (tools/documentum mock)."""
import uuid

import pytest

from tests.unit.test_documentum_client import dctm, dctm_server  # noqa: F401


def _cfg(dctm, **extra):
    return {"rest_url": dctm["rest_url"], "repository": dctm["repository"], "allow_http": True, **extra}


def _wait_indexed(test_client, connection_id, headers, attempts=200):
    """Schema refresh runs as a background indexing job; wait for its terminal state."""
    import time
    for _ in range(attempts):
        r = test_client.get(f"/api/connections/{connection_id}/indexing", headers=headers)
        assert r.status_code == 200, r.text
        status = (r.json() or {}).get("status")
        if status not in ("running", "queued", "pending"):
            return r.json()
        time.sleep(0.05)
    raise AssertionError("indexing did not finish")


@pytest.mark.e2e
def test_connection_lifecycle_catalog_and_member_boundary(
    dctm, test_client, create_user, login_user, whoami, create_connection, get_connection,
    test_connection_connectivity, refresh_connection_schema, get_connection_tables,
):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org = whoami(token)["organizations"][0]["id"]
    secret = "unique-repo-secret-never-returned"
    dctm["mock"].USERS["alice"]["password"] = secret
    try:
        conn = create_connection(name="Documentum QA", type="documentum", config=_cfg(dctm, root_path="/Finance"),
                                 credentials={"username": "alice", "password": secret}, user_token=token, org_id=org)
        assert conn["type"] == "documentum"
        details = get_connection(connection_id=conn["id"], user_token=token, org_id=org)
        assert secret not in str(details)
        probe = test_connection_connectivity(connection_id=conn["id"], user_token=token, org_id=org)
        assert probe["success"] is True, probe
        refresh_connection_schema(connection_id=conn["id"], user_token=token, org_id=org)
        run = _wait_indexed(test_client, conn["id"], {"Authorization": f"Bearer {token}", "X-Organization-Id": org})
        assert run.get("error") in (None, ""), run
        tables = get_connection_tables(connection_id=conn["id"], user_token=token, org_id=org)
        assert {t["name"] for t in tables} == {"Invoices/INV-1001.pdf", "Invoices/INV-1002.pdf", "Reports/2026/Q1_revenue.csv",
                                               "Reports/2026/Q2_revenue.csv", "Reports/2026/board_summary.txt", "Reports/2026/regional_targets.xlsx"}
    finally:
        dctm["mock"].USERS["alice"]["password"] = "alice123"

    email = f"dctm-member-{uuid.uuid4().hex[:8]}@example.com"
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
def test_credentials_are_required_and_bad_auth_cannot_pass(dctm, test_client, create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org}
    r = test_client.post("/api/connections", json={"name": "No credentials", "type": "documentum", "config": _cfg(dctm),
                                                   "credentials": {}, "auth_policy": "system_only"}, headers=headers)
    assert r.status_code in (400, 422)
    r = test_client.post("/api/connections/test-params", json={"name": "Bad credentials", "type": "documentum", "config": _cfg(dctm),
                                                               "credentials": {"username": "alice", "password": "incorrect"}}, headers=headers)
    assert r.status_code == 200 and r.json()["success"] is False, r.json()
    assert "incorrect" not in r.text
    r = test_client.post("/api/connections/test-params", json={"name": "Wrong repo", "type": "documentum",
                                                               "config": _cfg(dctm, repository="nope"),
                                                               "credentials": {"username": "alice", "password": "alice123"}}, headers=headers)
    assert r.status_code == 200 and r.json()["success"] is False


@pytest.mark.e2e
def test_user_required_impersonation_overlay_runs_as_each_member(
    dctm, test_client, create_user, login_user, whoami, create_data_source,
):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org}
    # The credential variant is selected by config.auth_type (as the connect form does).
    ds = create_data_source(name="Documentum per-user", type="documentum", config=_cfg(dctm, auth_type="otds_client"),
                            credentials={"otds_url": dctm["otds_url"], "client_id": "bow", "client_secret": "bow-secret", "partition": "Corp"},
                            auth_policy="user_required", user_token=token, org_id=org)
    ds_id = ds["id"]
    assert test_client.put(f"/api/data_sources/{ds_id}", json={"is_public": True}, headers=headers).status_code == 200
    connections = test_client.get(f"/api/data_sources/{ds_id}/connections", headers=headers).json()
    conn_id = connections[0]["id"]
    assert set(connections[0].get("allowed_user_auth_modes") or []) >= {"otds_impersonation", "oauth"}
    fields = test_client.get("/api/data_sources/documentum/fields", params={"auth_policy": "user_required"}, headers=headers).json()
    assert set(fields["credentials_by_auth"]) == {"userpass", "otds_impersonation", "oauth"}

    def member(login):
        email = f"dctm-{login}-{uuid.uuid4().hex[:6]}@example.com"
        r = test_client.post(f"/api/organizations/{org}/members", json={"organization_id": org, "email": email, "role": "member"}, headers=headers)
        assert r.status_code == 200, r.json()
        u = create_user(email=email)
        t = login_user(u["email"], u["password"])
        return {"Authorization": f"Bearer {t}", "X-Organization-Id": org}

    file_endpoint = f"/api/data_sources/{ds_id}/connections/{conn_id}/files"
    bob_h, alice_h = member("bob"), member("alice")
    dctm["mock"].REQUEST_LOG.clear()
    r = test_client.get(file_endpoint, headers=bob_h)
    assert r.status_code == 403 or r.json().get("connect_required") is True, r.json()
    assert not [u for u, _ in dctm["mock"].REQUEST_LOG if u], "no Documentum call may run before the member connects"

    endpoint = f"/api/connections/{conn_id}/my-credentials"
    r = test_client.post(endpoint, json={"auth_mode": "oauth", "credentials": {}}, headers=bob_h)
    assert r.status_code == 400
    r = test_client.post(endpoint, json={"auth_mode": "otds_impersonation", "credentials": {}}, headers=bob_h)
    assert r.status_code == 422
    r = test_client.post(endpoint, json={"auth_mode": "otds_impersonation", "credentials": {"documentum_login": "bob"}}, headers=bob_h)
    assert r.status_code == 200, r.json()
    assert "bow-secret" not in r.text
    probe = test_client.post(f"/api/connections/{conn_id}/test-my-credentials", headers=bob_h)
    assert probe.status_code == 200 and probe.json()["success"] is True, probe.json()
    r = test_client.post(endpoint, json={"auth_mode": "otds_impersonation", "credentials": {"documentum_login": "alice"}}, headers=alice_h)
    assert r.status_code == 200, r.json()

    dctm["mock"].REQUEST_LOG.clear()
    bob_files = test_client.get(file_endpoint, headers=bob_h)
    assert bob_files.status_code == 200, bob_files.json()
    bob_names = {f["name"] for f in bob_files.json()["files"]}
    assert bob_names and "Salary_Bands.csv" not in bob_names and "Travel_Policy.docx" not in bob_names
    assert {u for u, _ in dctm["mock"].REQUEST_LOG if u} == {"bob"}, "every call must run as the impersonated member"

    alice_files = test_client.get(file_endpoint, headers=alice_h)
    assert alice_files.status_code == 200
    assert {"Salary_Bands.csv", "Travel_Policy.docx"} <= {f["name"] for f in alice_files.json()["files"]}

    r = test_client.delete(endpoint, headers=bob_h)
    assert r.status_code in (200, 204), r.text
    dctm["mock"].REQUEST_LOG.clear()
    r = test_client.get(file_endpoint, headers=bob_h)
    assert r.status_code == 403 or r.json().get("connect_required") is True, r.json()
    assert not [u for u, _ in dctm["mock"].REQUEST_LOG if u]
