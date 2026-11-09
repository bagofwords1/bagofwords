"""Registry-created HPE OneView connections authenticate, verify version and index the catalog."""

import io
import json
from urllib.parse import urlsplit

import pytest
import requests


@pytest.mark.e2e
def test_hpe_oneview_connection_indexes_catalog_tables(
    create_user,
    login_user,
    whoami,
    create_data_source,
    refresh_schema,
    test_connection,
    delete_data_source,
    monkeypatch,
):
    wire = []

    def send(session, request, **kwargs):
        wire.append(request)
        path = urlsplit(request.url).path
        response = requests.Response()
        response.url = request.url
        if path == "/rest/version":
            assert request.method == "GET" and "X-API-Version" not in request.headers
            response.status_code, body = 200, {"currentVersion": 6800, "minimumVersion": 120}
        elif path == "/rest/login-sessions" and request.method == "POST":
            assert request.headers["X-API-Version"] == "6800"
            assert json.loads(request.body)["authLoginDomain"] == "corp.example"
            response.status_code, body = 200, {"partnerData": {}, "sessionID": "synthetic-session"}
        elif path == "/rest/login-sessions" and request.method == "DELETE":
            response.status_code, body = 204, None
        else:
            assert request.method == "GET" and request.headers["Auth"] == "synthetic-session"
            assert path == "/rest/appliance/nodeinfo/version"
            response.status_code, body = 200, {"softwareVersion": "9.00.00-0417420", "modelNumber": "HPE Synergy Composer2", "serialNumber": "CMP11"}
        response.raw = io.BytesIO(b"" if body is None else json.dumps(body).encode())
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    source = create_data_source(
        name="OneView RCA",
        type="hpe_oneview",
        config={"url": "https://oneview.example", "auth_login_domain": "corp.example"},
        credentials={"username": "reader", "password": "synthetic-only"},
        user_token=token,
        org_id=org,
    )
    result = test_connection(data_source_id=source["id"], user_token=token, org_id=org)
    assert result["success"]
    methods = [r.method for r in wire]
    # Every session is version → login → one GET → logout, however many probes the API ran.
    assert methods and len(methods) % 4 == 0 and methods == ["GET", "POST", "GET", "DELETE"] * (len(methods) // 4)
    calls_before_refresh = len(wire)
    tables = refresh_schema(data_source_id=source["id"], user_token=token, org_id=org)
    names = {t["name"] for t in tables}
    assert {"server_hardware", "server_profiles", "enclosures", "interconnects", "alerts", "tasks", "server_hardware_utilization"} <= names
    servers = next(t for t in tables if t["name"] == "server_hardware")
    assert any(c["name"] == "mpHostInfo.mpHostName" for c in servers["columns"])
    assert servers["metadata_json"]["hpe_oneview"]["endpoint"] == "/rest/server-hardware"
    utilization = next(t for t in tables if t["name"] == "server_hardware_utilization")
    assert utilization["metadata_json"]["hpe_oneview"]["parents"] == ["id"]
    # Schema discovery is catalog driven: refreshing the schema makes no appliance calls.
    assert len(wire) == calls_before_refresh
    delete_data_source(data_source_id=source["id"], user_token=token, org_id=org)
