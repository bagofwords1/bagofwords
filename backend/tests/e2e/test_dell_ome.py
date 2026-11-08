"""Registry-created Dell OpenManage Enterprise connections retain the catalog schema metadata."""

import io
import json
import re
from urllib.parse import unquote, urlsplit

import pytest
import requests

SESSIONS = "/api/SessionService/Sessions"


@pytest.mark.e2e
def test_dell_ome_connection_indexes_fleet_tables(
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
        response = requests.Response()
        path = unquote(urlsplit(request.url).path)
        if request.method == "POST":
            assert path == SESSIONS and json.loads(request.body)["SessionType"] == "API"
            response.status_code = 201
            response.headers["X-Auth-Token"] = "synthetic-token"
            response.raw = io.BytesIO(json.dumps({"Id": "sess-1", "UserName": "reader"}).encode())
            return response
        if request.method == "DELETE":
            assert re.fullmatch(re.escape(SESSIONS) + r"\('sess-1'\)", path)
            response.status_code = 204
            response.raw = io.BytesIO(b"")
            return response
        assert request.method == "GET" and path == "/api/ApplicationService/Info"
        assert request.headers["X-Auth-Token"] == "synthetic-token"
        response.status_code = 200
        response.raw = io.BytesIO(json.dumps({"Name": "OpenManage Enterprise", "Version": "4.1.0", "Guid": "synthetic-guid"}).encode())
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    source = create_data_source(
        name="OME fleet",
        type="dell_ome",
        config={"url": "https://ome.example"},
        credentials={"username": "reader", "password": "synthetic-only"},
        user_token=token,
        org_id=org,
    )
    result = test_connection(data_source_id=source["id"], user_token=token, org_id=org)
    assert result["success"]
    # Every probe is a complete handshake: login, one read of /Info, logout.
    assert seen and [r.method for r in seen] == ["POST", "GET", "DELETE"] * (len(seen) // 3)
    probes = len(seen)
    tables = refresh_schema(data_source_id=source["id"], user_token=token, org_id=org)
    names = {t["name"] for t in tables}
    assert {"devices", "alerts", "warranties", "firmware_baselines", "firmware_compliance_reports", "jobs", "groups", "audit_logs"} <= names
    devices = next(t for t in tables if t["name"] == "devices")
    assert any(c["name"] == "DeviceServiceTag" for c in devices["columns"])
    assert devices["metadata_json"]["dell_ome"]["endpoint"] == "/api/DeviceService/Devices"
    compliance = next(t for t in tables if t["name"] == "firmware_compliance_reports")
    assert compliance["metadata_json"]["dell_ome"]["parents"] == ["BaselineId"]
    # Schema discovery is catalog-driven: no further appliance calls were made.
    assert len(seen) == probes
    delete_data_source(data_source_id=source["id"], user_token=token, org_id=org)
