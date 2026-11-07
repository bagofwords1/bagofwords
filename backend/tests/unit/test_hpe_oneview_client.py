"""HPE OneView contracts exercised at the HTTP boundary; no appliance credentials.

Every test mocks `requests.Session.send` (the wire) or runs the in-repo
simulator; nothing asserts on private helpers.
"""

import importlib.util
import io
import json
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from app.data_sources.clients.hpe_oneview_client import HpeOneViewClient, OneViewQueryError
from app.schemas.data_source_registry import resolve_client_class

VERSION = {"currentVersion": 6800, "minimumVersion": 120}
LOGIN = {"partnerData": {}, "sessionID": "synthetic-session-token"}
PRIMARY_TABLES = [
    "appliance_version", "appliance_health", "server_hardware", "server_hardware_firmware", "server_hardware_utilization",
    "server_hardware_types", "server_profiles", "server_profile_templates", "enclosures", "enclosure_groups",
    "logical_enclosures", "interconnects", "interconnect_ports", "logical_interconnects", "sas_interconnects",
    "uplink_sets", "ethernet_networks", "fc_networks", "network_sets", "racks", "datacenters", "power_devices",
    "alerts", "events", "tasks", "firmware_drivers", "storage_systems", "storage_volumes", "storage_pools", "scopes",
    "index_resources",
]


def test_registry_resolves_client_and_catalog_tables():
    cls = resolve_client_class("hpe_oneview")
    assert cls is HpeOneViewClient
    client = cls(url="https://oneview.example", username="reader", password="test-only")
    tables = {t.name: t for t in client.get_tables()}
    assert set(PRIMARY_TABLES) <= tables.keys()
    assert tables["server_hardware_utilization"].metadata_json["hpe_oneview"]["parents"] == ["id"]
    assert any(c.name == "mpHostInfo.mpHostName" for c in tables["server_hardware"].columns)
    assert any(fk.references_name == "server_profiles" for fk in tables["server_hardware"].fks)
    assert "server_hardware" in client.prompt_schema() and len(client.prompt_schema()) < 12000


@pytest.fixture
def client():
    c = HpeOneViewClient(url="https://oneview.example", username="reader", password="test-only")
    yield c
    c.close()


@pytest.fixture
def http(monkeypatch):
    """Queue of (status, body) for data requests; the handshake is answered automatically.

    `seen` records every request (handshake included) so tests can assert the wire.
    """
    queue = []
    seen = []

    def send(session, request, **kwargs):
        seen.append(request)
        path = urlsplit(request.url).path
        r = requests.Response()
        r.url = request.url
        if path == "/rest/version" and request.method == "GET":
            r.status_code, body = 200, VERSION
        elif path == "/rest/login-sessions" and request.method == "POST":
            r.status_code, body = 200, LOGIN
        elif path == "/rest/login-sessions" and request.method == "DELETE":
            r.status_code, body = 204, None
        else:
            assert request.method == "GET", request.method
            r.status_code, body = queue.pop(0)
        r.raw = io.BytesIO(b"" if body is None else json.dumps(body).encode())
        return r

    monkeypatch.setattr(requests.Session, "send", send)
    return queue, seen


def data_requests(seen):
    return [r for r in seen if urlsplit(r.url).path not in {"/rest/version", "/rest/login-sessions"}]


def page(members, nxt=None, total=None, uri=None):
    return {"members": members, "start": 0, "count": len(members), "total": len(members) if total is None else total,
            "nextPageUri": nxt, "prevPageUri": None, "uri": uri, "category": "x", "type": "x-list"}


# ----------------------------------------------------------------------------- handshake
def test_session_handshake_negotiates_version_logs_in_and_out(client, http):
    http[0].append((200, page([{"uri": "/rest/server-hardware/a", "name": "a"}])))
    client.execute_query({"table": "server_hardware"})
    seen = http[1]
    assert [(r.method, urlsplit(r.url).path) for r in seen] == [
        ("GET", "/rest/version"), ("POST", "/rest/login-sessions"), ("GET", "/rest/server-hardware"), ("DELETE", "/rest/login-sessions")
    ]
    version_req, login_req, data_req, logout_req = seen
    assert "X-API-Version" not in version_req.headers
    assert login_req.headers["X-API-Version"] == "6800"
    assert json.loads(login_req.body) == {"userName": "reader", "password": "test-only", "authLoginDomain": "local", "loginMsgAck": True}
    assert "Auth" not in login_req.headers
    assert data_req.headers["Auth"] == "synthetic-session-token"
    assert "Authorization" not in data_req.headers
    assert data_req.headers["Accept"] == "application/json"
    assert logout_req.headers["Auth"] == "synthetic-session-token"


def test_login_is_attempted_even_when_query_fails_and_logout_still_happens(client, http):
    http[0].append((404, {"errorCode": "RESOURCE_NOT_FOUND", "message": "customer path"}))
    with pytest.raises(OneViewQueryError) as e:
        client.execute_query({"table": "racks"})
    assert e.value.code == "UnsupportedResource" and e.value.oneview_code == "RESOURCE_NOT_FOUND"
    assert "customer path" not in str(e.value)
    assert [r.method for r in http[1]][-1] == "DELETE"


def test_negotiated_version_is_clamped_to_catalog_range(client, monkeypatch):
    seen = []

    def send(session, request, **kwargs):
        seen.append(request)
        r = requests.Response()
        r.url = request.url
        path = urlsplit(request.url).path
        body = {"currentVersion": 9000, "minimumVersion": 120} if path == "/rest/version" else LOGIN if request.method == "POST" else page([])
        r.status_code = 204 if request.method == "DELETE" else 200
        r.raw = io.BytesIO(json.dumps(body).encode())
        return r

    monkeypatch.setattr(requests.Session, "send", send)
    client.execute_query({"table": "scopes"})
    assert seen[1].headers["X-API-Version"] == "7200"


@pytest.mark.parametrize("current,minimum,pinned", [(800, 120, None), (6800, 3000, 2400), (6800, 120, 7000)])
def test_version_outside_supported_range_is_rejected_before_login(monkeypatch, current, minimum, pinned):
    seen = []

    def send(session, request, **kwargs):
        seen.append(request)
        r = requests.Response()
        r.url, r.status_code = request.url, 200
        r.raw = io.BytesIO(json.dumps({"currentVersion": current, "minimumVersion": minimum}).encode())
        return r

    monkeypatch.setattr(requests.Session, "send", send)
    c = HpeOneViewClient(url="https://oneview.example", username="r", password="p", api_version=pinned)
    with pytest.raises(OneViewQueryError) as e:
        c.test_connection()
    assert e.value.code == "UnsupportedVersion"
    assert [r.method for r in seen] == ["GET"]


def test_bad_credentials_map_to_authentication_failed_without_echo(client, monkeypatch):
    def send(session, request, **kwargs):
        r = requests.Response()
        r.url = request.url
        if urlsplit(request.url).path == "/rest/version":
            r.status_code, body = 200, VERSION
        else:
            r.status_code, body = 400, {"errorCode": "AUTHN_AUTH_FAIL", "message": "Invalid username or password.", "details": "secret-detail"}
        r.raw = io.BytesIO(json.dumps(body).encode())
        return r

    monkeypatch.setattr(requests.Session, "send", send)
    with pytest.raises(OneViewQueryError) as e:
        client.test_connection()
    assert e.value.code == "AuthenticationFailed" and e.value.oneview_code == "AUTHN_AUTH_FAIL"
    assert "secret-detail" not in str(e.value) and "test-only" not in str(e.value)


def test_test_connection_reports_version_and_table_count(client, http):
    http[0].append((200, {"softwareVersion": "9.00.00-0417420", "modelNumber": "HPE Synergy Composer2", "serialNumber": "CMP11"}))
    result = client.test_connection()
    assert result["success"]
    assert "9.00.00-0417420" in result["message"] and "6800" in result["message"]
    assert str(len(client.get_tables())) in result["message"]


@pytest.mark.parametrize("body", [{}, {"softwareVersion": ""}, {"softwareVersion": None}])
def test_missing_appliance_version_is_not_connected(client, http, body):
    http[0].append((200, body))
    with pytest.raises(OneViewQueryError) as e:
        client.test_connection()
    assert e.value.code == "InvalidResponse"


# ----------------------------------------------------------------------------- pagination
def test_pagination_follows_next_page_uri_through_short_and_empty_pages(client, http):
    http[0].extend([
        (200, page([{"uri": "/rest/alerts/1", "severity": "Critical"}], nxt="/rest/alerts?start=1&count=100", total=3)),
        (200, page([], nxt="/rest/alerts?start=2&count=100", total=3)),
    ])
    df = client.execute_query({"table": "alerts", "fields": ["uri", "severity"]})
    # An empty page terminates paging even with a continuation; one row, two requests.
    assert len(df) == 1 and len(data_requests(http[1])) == 2
    first = parse_qs(urlsplit(data_requests(http[1])[0].url).query)
    assert first["start"] == ["0"] and int(first["count"][0]) <= 100


def test_pagination_collects_every_member_and_stops_at_null_next(client, http):
    http[0].extend([
        (200, page([{"uri": f"/rest/tasks/{i}"} for i in range(3)], nxt="/rest/tasks?start=3&count=100", total=5)),
        (200, page([{"uri": f"/rest/tasks/{i}"} for i in range(3, 5)], nxt=None, total=5)),
    ])
    df = client.execute_query({"table": "tasks", "fields": ["uri"]})
    assert list(df["uri"]) == [f"/rest/tasks/{i}" for i in range(5)]
    assert df["_bow_query_id"].nunique() == 1
    assert parse_qs(urlsplit(data_requests(http[1])[1].url).query)["start"] == ["3"]


@pytest.mark.parametrize(
    "nxt",
    [
        "https://evil.example/rest/alerts?start=1",
        "/rest/users?start=1",
        "/rest/alerts?start=1&filter=%22severity%3D%27OK%27%22",
        "/rest/alerts?start=1&view=tree",
        "/rest/alerts?start=0",
    ],
)
def test_continuation_cannot_escape_origin_resource_or_scope(client, http, nxt):
    http[0].append((200, page([{"uri": "/rest/alerts/1"}], nxt=nxt, total=2)))
    with pytest.raises(OneViewQueryError) as e:
        client.execute_query({"table": "alerts"})
    assert e.value.code == "InvalidResponse"
    assert len(data_requests(http[1])) == 1


def test_continuation_keeps_filters_and_sort_from_the_original_request(client, http):
    http[0].extend([
        (200, page([{"uri": "/rest/alerts/1"}], nxt="/rest/alerts?start=1&count=100&filter=%22alertState%3D%27Active%27%22&sort=created%3Adesc", total=2)),
        (200, page([{"uri": "/rest/alerts/2"}], total=2)),
    ])
    df = client.execute_query({"table": "alerts", "filter": {"alertState": "Active"}, "sort": ["created desc"]})
    assert len(df) == 2
    for r in data_requests(http[1]):
        q = parse_qs(urlsplit(r.url).query)
        assert q["filter"] == ["\"alertState='Active'\""] and q["sort"] == ["created:desc"]


def test_limit_never_silently_truncates(client, http):
    http[0].extend([
        (200, page([{"uri": "/rest/alerts/1"}], nxt="/rest/alerts?start=1&count=1", total=None)),
        (200, page([{"uri": "/rest/alerts/2"}], total=None)),
    ])
    with pytest.raises(OneViewQueryError) as e:
        client.execute_query({"table": "alerts", "limit": 1})
    assert e.value.code == "ResultLimitExceeded"


def test_reported_total_above_limit_fails_fast_without_paging(client, http):
    http[0].append((200, page([{"uri": "/rest/alerts/1"}], nxt="/rest/alerts?start=1&count=1", total=5000)))
    with pytest.raises(OneViewQueryError) as e:
        client.execute_query({"table": "alerts", "limit": 10})
    assert e.value.code == "ResultLimitExceeded"
    assert len(data_requests(http[1])) == 1


def test_count_minus_one_is_never_sent(client, http):
    http[0].append((200, page([])))
    client.execute_query({"table": "enclosures", "limit": 5})
    q = parse_qs(urlsplit(data_requests(http[1])[0].url).query)
    assert q["count"] == ["5"] and q["start"] == ["0"]


# ----------------------------------------------------------------------------- errors
@pytest.mark.parametrize(
    "status,code", [(401, "AuthenticationFailed"), (403, "PermissionDenied"), (404, "UnsupportedResource"), (412, "UnsupportedVersion")]
)
def test_http_errors_map_to_codes_without_body_leakage(client, http, status, code):
    http[0].append((status, {"errorCode": "AUTHORIZATION", "message": "sensitive customer text", "details": "more secrets"}))
    with pytest.raises(OneViewQueryError) as e:
        client.execute_query({"table": "server_profiles"})
    assert e.value.code == code and e.value.status == status
    assert e.value.oneview_code == "AUTHORIZATION"
    assert "sensitive" not in str(e.value) and "secrets" not in str(e.value)


def test_error_envelope_with_200_is_not_data(client, http):
    http[0].append((200, {"errorCode": "INVALID_ARGUMENT", "message": "x"}))
    with pytest.raises(OneViewQueryError) as e:
        client.execute_query({"table": "server_profiles"})
    assert e.value.code == "InvalidResponse"


def test_transient_errors_retry_then_return_complete_data(client, http, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    http[0].extend([(503, {}), (200, page([{"uri": "/rest/racks/1"}]))])
    df = client.execute_query({"table": "racks"})
    assert len(df) == 1 and len(data_requests(http[1])) == 2


# ----------------------------------------------------------------------------- validation (no HTTP)
@pytest.mark.parametrize(
    "query",
    [
        {"table": "users"},
        {"table": "server_hardware", "path": "/rest/users"},
        {"table": "server_hardware", "fields": ["password"]},
        {"table": "server_hardware", "fields": []},
        {"table": "server_hardware", "filter": {"uri": "/rest/x"}},
        {"table": "server_hardware", "filter": {"name": "it's"}},
        {"table": "server_hardware", "filter": {"name": {"op": "regex", "value": "x"}}},
        {"table": "server_hardware", "filter": {"name": {"op": "in", "value": []}}},
        {"table": "server_hardware", "sort": ["password desc"]},
        {"table": "server_hardware", "sort": ["name sideways"]},
        {"table": "server_hardware", "limit": True},
        {"table": "server_hardware", "lookback": "24h"},
        {"table": "server_hardware", "parent": {"id": "x"}},
        {"table": "server_hardware_utilization"},
        {"table": "server_hardware_utilization", "parent": {"id": "../../login-sessions"}},
        {"table": "server_hardware_utilization", "parent": {"id": "a b"}},
        {"table": "server_hardware_utilization", "parent": {"id": "abc"}, "metrics": ["Voltage"]},
        {"table": "server_hardware_utilization", "parent": {"id": "abc"}, "view": "minute"},
        {"table": "server_hardware_utilization", "parent": {"id": "abc"}, "lookback": "1h", "start_time": "2024-01-01T00:00:00Z"},
        {"table": "alerts", "lookback": "yesterday"},
        {"table": "alerts", "start_time": "2024-01-01T00:00:00", "end_time": "2024-01-02T00:00:00Z"},
        {"table": "alerts", "lookback": "1h", "filter": {"created": "2024"}},
        {"table": "alerts", "metrics": ["AveragePower"]},
        {"table": "index_resources", "category": ["Server Hardware"]},
        {"table": "index_resources", "user_query": ""},
        {"table": "appliance_version", "filter": {"name": "x"}},
        {"table": "appliance_version", "sort": ["name"]},
        "not json",
        42,
    ],
)
def test_invalid_or_unsafe_queries_make_no_http_request(client, http, query):
    with pytest.raises(OneViewQueryError) as e:
        client.execute_query(query)
    assert e.value.code == "InvalidQuery"
    assert not http[1]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"url": "http://oneview.example", "allow_http": False},
        {"url": "http://10.0.0.5", "allow_http": True},
        {"url": "https://oneview.example/rest"},
        {"url": "https://user:pw@oneview.example"},
        {"url": "https://oneview.example", "api_version": 100},
        {"url": "https://oneview.example", "auth_login_domain": "corp domain"},
        {"url": "https://oneview.example", "timeout": 0},
        {"url": "https://oneview.example", "password": ""},
    ],
)
def test_invalid_configuration_is_rejected_at_construction(kwargs):
    params = {"username": "reader", "password": "test-only", **kwargs}
    with pytest.raises(OneViewQueryError) as e:
        HpeOneViewClient(**params)
    assert e.value.code in {"InvalidConfiguration", "InvalidQuery"}


def test_client_cannot_issue_a_mutating_request(client, http):
    """The only network primitive refuses anything but GET on catalog paths and the login handshake."""
    import requests as rq

    with rq.Session() as s:
        for method, path in [("PUT", "/rest/server-hardware/a/powerState"), ("POST", "/rest/alerts"), ("PATCH", "/rest/tasks/1"),
                             ("DELETE", "/rest/server-profiles/x"), ("GET", "/rest/users"), ("GET", "/rest/login-sessions"),
                             ("POST", "/rest/users"), ("GET", "/rest/server-hardware/../users")]:
            with pytest.raises(OneViewQueryError) as e:
                client._request(s, method, path, 1e12)
            assert e.value.code == "ForbiddenOperation", (method, path)
    assert not http[1]


# ----------------------------------------------------------------------------- query shapes per table
def test_server_hardware_filters_and_sort_use_oneview_syntax(client, http):
    http[0].append((200, page([])))
    client.execute_query({
        "table": "server_hardware",
        "filter": {"status": "Critical", "memoryMb": {"op": "ge", "value": 262144}, "name": {"op": "like", "value": "Frame-1%"},
                   "powerState": {"op": "in", "value": ["On", "PoweringOn"]}, "maintenanceMode": False},
        "sort": ["name asc", "status desc"],
    })
    q = parse_qs(urlsplit(data_requests(http[1])[0].url).query)
    assert q["filter"] == [
        "\"status='Critical'\"", "\"memoryMb>=262144\"", "\"name matches 'Frame-1%'\"",
        "\"powerState='On' OR powerState='PoweringOn'\"", "\"maintenanceMode=false\"",
    ]
    assert q["sort"] == ["name:asc,status:desc"]
    assert "fields" not in q


def test_time_window_becomes_created_bounds_on_activity_tables(client, http):
    http[0].append((200, page([])))
    before = datetime.now(UTC)
    client.execute_query({"table": "events", "lookback": "6h"})
    q = parse_qs(urlsplit(data_requests(http[1])[0].url).query)
    bounds = [f for f in q["filter"] if f.startswith("\"created")]
    assert len(bounds) == 2
    lower = datetime.fromisoformat(bounds[0].split("'")[1].replace("Z", "+00:00"))
    assert timedelta(hours=5, minutes=59) <= before - lower <= timedelta(hours=6, minutes=1)


def test_utilization_request_and_flattening(client, http):
    http[0].append((200, {
        "metricList": [
            {"metricName": "AveragePower", "metricSamples": [[1700000000000, 410.5], [1700003600000, None]], "metricCapacity": 800},
            {"metricName": "AmbientTemperature", "metricSamples": [[1700000000000, 22]], "metricCapacity": None},
        ],
        "resolution": 3600000, "sliceStartTime": "2023-11-14T22:00:00.000Z", "sliceEndTime": "2023-11-15T22:00:00.000Z",
        "newestSampleTime": "2023-11-15T22:00:00.000Z", "oldestSampleTime": "2023-10-15T00:00:00.000Z", "isFresh": True, "refreshTaskUri": None,
    }))
    df = client.execute_query({
        "table": "server_hardware_utilization", "parent": {"id": "srv-1"}, "metrics": ["AveragePower", "AmbientTemperature"],
        "start_time": "2023-11-14T22:00:00Z", "end_time": "2023-11-15T22:00:00Z", "view": "hour",
    })
    req = data_requests(http[1])[0]
    assert urlsplit(req.url).path == "/rest/server-hardware/srv-1/utilization"
    q = parse_qs(urlsplit(req.url).query)
    assert q["fields"] == ["AveragePower,AmbientTemperature"] and q["view"] == ["hour"]
    assert q["filter"] == ["startDate=2023-11-14T22:00:00.000Z,endDate=2023-11-15T22:00:00.000Z"]
    assert len(df) == 3 and list(df["metricName"]) == ["AveragePower", "AveragePower", "AmbientTemperature"]
    assert df.iloc[0]["value"] == 410.5 and pd_isna(df.iloc[1]["value"])
    assert str(df["value"].dtype) == "Float64" and df.iloc[0]["sampleTime"].startswith("2023-11-14T22:13:20")
    assert set(df["id"]) == {"srv-1"}


def pd_isna(value):
    import pandas as pd

    return bool(pd.isna(value))


def test_firmware_without_parent_reads_estate_collection_and_flattens_components(client, http):
    http[0].append((200, page([{
        "uri": "/rest/server-hardware/a/firmware", "serverHardwareUri": "/rest/server-hardware/a", "serverName": "s1", "serverModel": "SY 480",
        "components": [{"componentName": "System ROM", "componentVersion": "v2", "componentLocation": "System Board", "componentKey": "rom"},
                       {"componentName": "iLO5", "componentVersion": "3.03", "componentLocation": "System Board", "componentKey": "ilo"}],
    }])))
    df = client.execute_query({"table": "server_hardware_firmware", "filter": {"components.componentName": "System ROM"}})
    req = data_requests(http[1])[0]
    assert urlsplit(req.url).path == "/rest/server-hardware/*/firmware"
    assert len(df) == 2 and list(df["component.componentName"]) == ["System ROM", "iLO5"]
    assert set(df["serverHardwareUri"]) == {"/rest/server-hardware/a"}


def test_firmware_with_parent_reads_one_server(client, http):
    http[0].append((200, {"uri": "/rest/server-hardware/a/firmware", "serverHardwareUri": "/rest/server-hardware/a", "serverName": "s1",
                          "components": [{"componentName": "System ROM", "componentVersion": "v2"}]}))
    df = client.execute_query({"table": "server_hardware_firmware", "parent": {"id": "a"}})
    req = data_requests(http[1])[0]
    assert urlsplit(req.url).path == "/rest/server-hardware/a/firmware" and not urlsplit(req.url).query
    assert len(df) == 1 and df.iloc[0]["id"] == "a" and df.iloc[0]["component.componentVersion"] == "v2"


def test_interconnect_ports_flatten_with_owner_identity(client, http):
    http[0].append((200, page([
        {"uri": "/rest/interconnects/i1", "name": "ic1", "enclosureName": "Frame-1",
         "ports": [{"portName": "Q1", "portStatus": "Linked", "neighbor": {"remoteSystemName": "tor-a"}}, {"portName": "Q2", "portStatus": "Unlinked", "neighbor": None}]},
        {"uri": "/rest/interconnects/i2", "name": "ic2", "enclosureName": "Frame-1", "ports": []},
    ])))
    df = client.execute_query({"table": "interconnect_ports", "fields": ["interconnectUri", "interconnectName", "port.portName", "port.portStatus", "port.neighbor"]})
    assert len(df) == 2 and set(df["interconnectUri"]) == {"/rest/interconnects/i1"}
    assert df.iloc[0]["port.neighbor"] == {"remoteSystemName": "tor-a"} and list(df["port.portName"]) == ["Q1", "Q2"]


def test_index_search_sends_category_and_user_query(client, http):
    http[0].append((200, page([{"uri": "/rest/server-hardware/a", "name": "s", "category": "server-hardware", "attributes": {"serialNumber": "CZ1"}}])))
    df = client.execute_query({"table": "index_resources", "category": ["server-hardware", "enclosures"], "user_query": "CZ1"})
    q = parse_qs(urlsplit(data_requests(http[1])[0].url).query)
    assert q["category"] == ["server-hardware", "enclosures"] and q["userQuery"] == ["CZ1"]
    assert df.iloc[0]["attributes"]["serialNumber"] == "CZ1"


def test_appliance_health_rows_keep_raw_member_for_unmodelled_fields(client, http):
    http[0].append((200, {"members": [{"resourceName": "CPU", "status": "OK", "coresUsed": 3}, {"resourceName": "Disk", "status": "Warning"}]}))
    df = client.execute_query({"table": "appliance_health"})
    assert list(df["resourceName"]) == ["CPU", "Disk"] and df.iloc[0]["member"]["coresUsed"] == 3
    assert not urlsplit(data_requests(http[1])[0].url).query


@pytest.mark.parametrize("table", [t for t in PRIMARY_TABLES if t not in {"server_hardware_utilization", "appliance_health", "appliance_version"}])
def test_every_collection_table_builds_a_bounded_get_on_its_catalog_path(client, http, table):
    http[0].append((200, page([])))
    df = client.execute_query({"table": table})
    req = data_requests(http[1])[0]
    meta = client.get_schema(table).metadata_json["hpe_oneview"]
    assert req.method == "GET" and urlsplit(req.url).path == meta["endpoint"]
    q = parse_qs(urlsplit(req.url).query)
    assert set(q) == {"start", "count"}
    assert df.empty and str(df["_bow_retrieved_at"].dtype) == "datetime64[ns, UTC]"
    assert {"uri" if "uri" in meta["default_fields"] else meta["default_fields"][0]} <= set(df.columns)


def test_dtypes_follow_catalog_and_nested_values_stay_structured(client, http):
    http[0].append((200, page([
        {"uri": "/rest/server-hardware/a", "name": "a", "memoryMb": 393216, "maintenanceMode": False,
         "mpHostInfo": {"mpHostName": "ilo-a", "mpIpAddresses": [{"address": "10.0.0.1", "type": "DHCP"}]}, "position": None},
        {"uri": "/rest/server-hardware/b", "name": "b", "memoryMb": None, "maintenanceMode": None, "mpHostInfo": None, "position": 4},
    ])))
    df = client.execute_query({"table": "server_hardware", "fields": ["name", "memoryMb", "maintenanceMode", "mpHostInfo", "position"]})
    assert str(df["memoryMb"].dtype) == "Int64" and str(df["maintenanceMode"].dtype) == "boolean" and str(df["name"].dtype) == "string"
    assert df.iloc[0]["memoryMb"] == 393216 and pd_isna(df.iloc[1]["memoryMb"]) and df.iloc[1]["position"] == 4
    assert df.iloc[0]["mpHostInfo.mpHostName"] == "ilo-a" and df.iloc[0]["mpHostInfo.mpIpAddresses"][0]["address"] == "10.0.0.1"
    assert pd_isna(df.iloc[1]["mpHostInfo.mpHostName"])
    assert df.iloc[0]["_bow_api_version"] == 6800 and df.iloc[0]["_bow_endpoint"] == "/rest/server-hardware"


def test_star_selects_every_catalog_field(client, http):
    http[0].append((200, page([{"uri": "/rest/scopes/a", "name": "a"}])))
    df = client.execute_query({"table": "scopes", "fields": "*"})
    assert set(client.get_schema("scopes").metadata_json["hpe_oneview"]["default_fields"]) < set(df.columns)
    assert "applianceId" in df.columns


def test_secret_like_keys_are_stripped_from_nested_values(client, http):
    http[0].append((200, page([{
        "uri": "/rest/storage-systems/a", "name": "arr", "credentials": {"username": "u", "password": "sensitive"},
        "deviceSpecificAttributes": {"model": "Primera", "apiToken": "sensitive", "nested": {"secretKey": "sensitive"}},
        "ports": [{"id": "0:3:1", "snmpCommunity": "sensitive"}],
    }])))
    df = client.execute_query({"table": "storage_systems", "fields": "*"})
    assert "sensitive" not in df.to_json()
    assert df.iloc[0]["deviceSpecificAttributes"]["model"] == "Primera" and df.iloc[0]["ports"][0]["id"] == "0:3:1"


def test_query_alias_and_async_wrapper_share_the_contract(client, http):
    import asyncio

    http[0].extend([(200, page([])), (200, page([]))])
    assert client.query({"table": "racks"}).empty
    assert asyncio.run(client.aexecute_query({"table": "racks"})).empty


def test_rca_query_runs_through_bow_code_executor(client, http):
    from app.ai.code_execution.code_execution import StreamingCodeExecutor

    http[0].append((200, page([
        {"uri": "/rest/server-hardware/a", "name": "a", "status": "Critical", "serverProfileUri": "/rest/server-profiles/p1"},
        {"uri": "/rest/server-hardware/b", "name": "b", "status": "OK", "serverProfileUri": None},
    ])))
    code = """
def generate_df(ds_clients, excel_files):
    df = ds_clients["ov"].execute_query({"table": "server_hardware", "fields": ["name", "status", "serverProfileUri"]})
    return df[df["status"] != "OK"]
"""
    client._bow_connection_id = "synthetic-connection"
    frame, _, queries = StreamingCodeExecutor().execute_code(code=code, ds_clients={"ov": client}, excel_files=[])
    assert list(frame["name"]) == ["a"] and set(frame["_bow_connection"]) == {"synthetic-connection"}
    assert len(queries) == 1 and json.loads(queries[0])["table"] == "server_hardware"


def test_malformed_catalog_is_rejected_before_any_http(client, http, monkeypatch):
    from app.data_sources.clients.hpe_oneview_client import _catalog

    original = Path.read_text
    catalog = json.loads(original(Path(__file__).parents[2] / "app/data_sources/clients/hpe_oneview/catalog.json"))
    catalog["resources"]["server_hardware_utilization"]["parents"] = []

    def read(path, *args, **kwargs):
        if path.name == "catalog.json" and path.parent.name == "hpe_oneview":
            return json.dumps(catalog)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    _catalog.cache_clear()
    try:
        with pytest.raises(OneViewQueryError) as e:
            client.get_tables()
        assert e.value.code == "InvalidCatalog" and not http[1]
    finally:
        _catalog.cache_clear()


# ----------------------------------------------------------------------------- simulator round trip
_SIM = Path(__file__).parents[3] / "tools/hpe_oneview/simulated_api.py"


def _load_simulator():
    if "hpe_oneview_simulated_api" in sys.modules:
        return sys.modules["hpe_oneview_simulated_api"]
    spec = importlib.util.spec_from_file_location("hpe_oneview_simulated_api", _SIM)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpe_oneview_simulated_api"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def simulator(request):
    from http.server import ThreadingHTTPServer

    mp = pytest.MonkeyPatch()
    mp.setenv("ONEVIEW_USERNAME", "sim-reader")
    mp.setenv("ONEVIEW_PASSWORD", "sim-only-password")
    mock = _load_simulator()
    server = ThreadingHTTPServer(("127.0.0.1", 0), mock.make_handler(mock.Simulator()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    mp.undo()


def test_real_client_against_simulator_over_http(simulator):
    c = HpeOneViewClient(url=simulator, username="sim-reader", password="sim-only-password", allow_http=True)
    result = c.test_connection()
    assert result["success"] and "9.00.00" in result["message"]
    with pytest.raises(OneViewQueryError) as e:
        HpeOneViewClient(url=simulator, username="sim-reader", password="wrong", allow_http=True).test_connection()
    assert e.value.code == "AuthenticationFailed" and e.value.oneview_code == "AUTHN_AUTH_FAIL"

    servers = c.execute_query({"table": "server_hardware", "fields": "*"})
    assert len(servers) == 24 and {"OK", "Warning", "Critical"} == set(servers["status"])
    sid = servers.iloc[0]["uri"].rsplit("/", 1)[-1]
    rows = {}
    for table in PRIMARY_TABLES:
        q = {"table": table}
        if table == "server_hardware_utilization":
            q.update(parent={"id": sid}, lookback="24h", view="hour")
        if table == "index_resources":
            q.update(category=["server-hardware"], user_query="Frame-2")
        rows[table] = c.execute_query(q)
    assert all(len(rows[t]) > 0 for t in PRIMARY_TABLES if t != "fcoe_networks"), {t: len(v) for t, v in rows.items()}
    assert len(rows["alerts"]) >= 70 and len(rows["tasks"]) >= 35 and len(rows["enclosures"]) == 3 and len(rows["interconnects"]) == 6
    assert rows["server_hardware_utilization"]["metricName"].nunique() == 6 and len(rows["server_hardware_utilization"]) >= 24 * 6

    critical = c.execute_query({"table": "alerts", "filter": {"alertState": "Active", "severity": "Critical"}, "lookback": "48h", "sort": ["created desc"]})
    assert len(critical) >= 3 and set(critical["severity"]) == {"Critical"} and set(critical["alertState"]) == {"Active"}
    created = list(critical["created"])
    assert created == sorted(created, reverse=True)
    noncompliant = c.execute_query({"table": "server_profiles", "filter": {"templateCompliance": "NonCompliant"}})
    assert len(noncompliant) == 3
    paged = c.execute_query({"table": "alerts", "limit": 1000, "fields": ["uri", "severity"]})
    assert paged["uri"].is_unique and len(paged) == len(rows["alerts"])
    with pytest.raises(OneViewQueryError) as e:
        c.execute_query({"table": "alerts", "limit": 10})
    assert e.value.code == "ResultLimitExceeded"
    ports = c.execute_query({"table": "interconnect_ports", "filter": {"status": "Critical"}})
    assert len(ports) == 20 and set(ports["interconnectName"]) == {"Frame-2, interconnect 6"}
    assert "REDACTED" not in c.execute_query({"table": "storage_systems", "fields": "*"}).to_json()
    assert "public-ro" not in c.execute_query({"table": "interconnects", "fields": ["snmpConfiguration"]}).to_json()
