"""Registry-created F5 BIG-IP connections authenticate with a token and index the read-only catalog."""

import io
import json
from urllib.parse import urlsplit

import pytest
import requests

LOCAL = "https://localhost"


@pytest.mark.e2e
def test_f5_bigip_connection_indexes_catalog_tables(
    create_user,
    login_user,
    whoami,
    create_data_source,
    refresh_schema,
    test_connection,
    delete_data_source,
    monkeypatch,
):
    seen = []

    def send(session, request, **kwargs):
        seen.append(request)
        path = urlsplit(request.url).path
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        if request.method == "POST" and path == "/mgmt/shared/authn/login":
            assert json.loads(request.body)["loginProviderName"] == "tmos"
            body = {"username": "reader", "token": {"token": "E2ETOKEN00000000000000000X", "timeout": 1200}}
        elif request.method == "DELETE" and path == "/mgmt/shared/authz/tokens/E2ETOKEN00000000000000000X":
            body = {}
        else:
            assert request.method == "GET" and request.headers["X-F5-Auth-Token"] == "E2ETOKEN00000000000000000X"
            if path == "/mgmt/tm/sys/version":
                body = {
                    "kind": "tm:sys:version:versionstats",
                    "entries": {f"{LOCAL}/mgmt/tm/sys/version/0": {"nestedStats": {"entries": {"Version": {"description": "17.1.1"}, "Product": {"description": "BIG-IP"}}}}},
                }
            elif path == "/mgmt/tm/cm/device":
                body = {"kind": "tm:cm:device:devicecollectionstate", "items": [{"fullPath": "/Common/bigip-a", "hostname": "bigip-a", "failoverState": "active", "selfDevice": "true"}]}
            else:
                raise AssertionError(f"unexpected request {request.method} {path}")
        response.raw = io.BytesIO(json.dumps(body).encode())
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    source = create_data_source(
        name="BIG-IP RCA",
        type="f5_bigip",
        config={"url": "https://bigip.example"},
        credentials={"username": "reader", "password": "synthetic-only"},
        user_token=token,
        org_id=org,
    )
    result = test_connection(data_source_id=source["id"], user_token=token, org_id=org)
    assert result["success"], result
    assert "17.1.1" in result["message"]
    assert [r.method for r in seen].count("POST") == [r.method for r in seen].count("DELETE") >= 1
    tables = refresh_schema(data_source_id=source["id"], user_token=token, org_id=org)
    names = {t["name"] for t in tables}
    assert {"virtual_servers", "virtual_server_stats", "pools", "pool_members", "pool_member_stats", "ssl_certs", "cm_devices", "cm_failover_status", "sys_version"} <= names
    members = next(t for t in tables if t["name"] == "pool_member_stats")
    assert any(c["name"] == "monitorStatus" for c in members["columns"])
    assert members["metadata_json"]["f5_bigip"]["endpoint"] == "/mgmt/tm/ltm/pool/{pool}/members/stats"
    assert members["metadata_json"]["f5_bigip"]["parents"] == ["pool"]
    delete_data_source(data_source_id=source["id"], user_token=token, org_id=org)
