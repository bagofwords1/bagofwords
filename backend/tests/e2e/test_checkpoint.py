"""Registry-created Check Point connections index the read-only catalog through the real API."""

import io
import json
from urllib.parse import urlsplit

import pytest
import requests


@pytest.mark.e2e
def test_checkpoint_connection_indexes_policy_tables(
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
        assert request.method == "POST"
        command = urlsplit(request.url).path.rsplit("/", 1)[-1]
        replies = {
            "login": {"sid": "synthetic-sid", "uid": "s-1", "url": "https://mgmt.example:443/web_api", "session-timeout": 600,
                      "api-server-version": "1.9.1", "read-only": True},
            "show-api-versions": {"current-version": "1.9.1", "supported-versions": ["1.8", "1.9", "1.9.1"]},
            "show-session": {"uid": "s-1", "type": "session", "user-name": "reader", "read-only": True, "connection-mode": "read only"},
            "logout": {"message": "OK"},
        }
        assert command in replies, command  # nothing but the identity check may run during connect/index
        if command != "login":
            assert request.headers["X-chkp-sid"] == "synthetic-sid"
        response = requests.Response()
        response.status_code = 200
        response.raw = io.BytesIO(json.dumps(replies[command]).encode())
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    source = create_data_source(
        name="Check Point RCA",
        type="checkpoint",
        config={"url": "https://mgmt.example", "domain": "", "api_version": ""},
        credentials={"api_key": "synthetic-only"},
        user_token=token,
        org_id=org,
    )
    result = test_connection(data_source_id=source["id"], user_token=token, org_id=org)
    assert result["success"]
    assert json.loads(seen[0].body) == {"api-key": "synthetic-only", "read-only": True}
    tables = refresh_schema(data_source_id=source["id"], user_token=token, org_id=org)
    names = {t["name"] for t in tables}
    assert {"hosts", "access_rules", "nat_rules", "gateways_and_servers", "sessions", "logs", "audit_logs"} <= names
    rules = next(t for t in tables if t["name"] == "access_rules")
    assert any(c["name"] == "rule-number" for c in rules["columns"])
    assert rules["metadata_json"]["checkpoint"]["command"] == "show-access-rulebase"
    assert rules["metadata_json"]["checkpoint"]["parents"] == ["layer"]
    delete_data_source(data_source_id=source["id"], user_token=token, org_id=org)
