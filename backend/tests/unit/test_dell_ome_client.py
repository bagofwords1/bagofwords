"""Dell OpenManage Enterprise contracts exercised at the HTTP boundary; no appliance credentials."""

import io
import json
import re
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import requests

from app.data_sources.clients.dell_ome_client import DellOmeClient, DellOmeError, _catalog
from app.schemas.data_source_registry import resolve_client_class

SESSIONS = "/api/SessionService/Sessions"
LOGIN_BODY = {"Id": "d5c28d8e-1084-4055-9c01-e1051cfee2dd", "Description": "reader", "Name": "API", "UserName": "reader",
              "UserId": 10078, "Password": None, "Roles": ["VIEWER"]}
MINIMUM_TABLES = {
    "appliance_info", "devices", "device_summary", "device_types", "device_inventory", "device_hardware_logs",
    "device_power", "device_temperature", "alerts", "alert_categories", "warranties", "firmware_baselines",
    "firmware_compliance_reports", "firmware_catalogs", "jobs", "job_execution_histories", "groups", "group_devices",
    "audit_logs", "templates", "configuration_baselines", "management_domains",
}


def _response(status, body=None, headers=None, url=""):
    r = requests.Response()
    r.status_code = status
    r.raw = io.BytesIO(b"" if body is None else json.dumps(body).encode())
    r.headers.update(headers or {})
    r.url = url
    return r


@pytest.fixture
def client():
    c = DellOmeClient(url="https://ome.example", username="reader", password="test-only")
    yield c
    c.close()


@pytest.fixture
def http(monkeypatch):
    """Queue of GET responses; the session handshake is answered automatically unless overridden."""
    state = {"queue": [], "seen": [], "login": (201, LOGIN_BODY, {"X-Auth-Token": "tok-123"}), "logout": 204}

    def send(session, request, **kwargs):
        state["seen"].append(request)
        path = unquote(urlsplit(request.url).path)
        if request.method == "POST":
            assert path == SESSIONS
            status, body, headers = state["login"]
            return _response(status, body, headers, request.url)
        if request.method == "DELETE":
            assert re.fullmatch(re.escape(SESSIONS) + r"\('[^']+'\)", path)
            return _response(state["logout"], None, None, request.url)
        assert request.method == "GET"
        if not state["queue"]:
            raise AssertionError(f"unexpected GET {request.url}")
        item = state["queue"].pop(0)
        status, body = item[0], item[1]
        headers = item[2] if len(item) > 2 else None
        return _response(status, body, headers, request.url)

    monkeypatch.setattr(requests.Session, "send", send)
    return state


def gets(state):
    return [r for r in state["seen"] if r.method == "GET"]


def collection(records, count=None, next_link=None, entity="DeviceService.Device"):
    body = {"@odata.context": f"/api/$metadata#Collection({entity})", "value": records}
    body["@odata.count"] = len(records) if count is None else count
    if next_link:
        body["@odata.nextLink"] = next_link
    return body


# ----------------------------------------------------------------------------- registry / catalog


def test_registry_resolves_client_and_catalog_exposes_minimum_tables(http):
    cls = resolve_client_class("dell_ome")
    client = cls(url="https://ome.example", username="reader", password="test-only")
    tables = {t.name: t for t in client.get_tables()}
    assert MINIMUM_TABLES <= tables.keys()
    assert not gets(http)
    meta = tables["firmware_compliance_reports"].metadata_json["dell_ome"]
    assert meta["parents"] == ["BaselineId"] and meta["endpoint"].startswith("/api/UpdateService/Baselines(")
    assert {"Component.CurrentVersion", "Component.Version", "DeviceId", "BaselineId"} <= {c.name for c in tables["firmware_compliance_reports"].columns}
    assert any(fk.references_name == "devices" for fk in tables["alerts"].fks)


def test_catalog_binds_every_placeholder_and_filters_are_fields(client):
    for table in client.get_tables():
        meta = table.metadata_json["dell_ome"]
        assert set(re.findall(r"\{([^}]+)\}", meta["endpoint"])) == set(meta["parents"]), table.name
        names = {c.name for c in table.columns}
        assert set(meta["filters"]) <= names, table.name
        assert set(meta["parents"]) | set(meta["optional_parents"]) <= names, table.name
    assert len(client.prompt_schema().splitlines()) == len(_catalog()["resources"])


@pytest.mark.parametrize("change", ["missing_parent", "filter_outside_fields", "broken_template", "bad_explode"])
def test_malformed_catalog_is_rejected_before_any_http(client, http, monkeypatch, change):
    original = Path.read_text
    catalog = json.loads(original(Path(__file__).parents[2] / "app/data_sources/clients/dell_ome/catalog.json"))
    resource = catalog["resources"]["firmware_compliance_reports"]
    if change == "missing_parent":
        resource["parents"] = {}
    elif change == "filter_outside_fields":
        catalog["resources"]["devices"]["filters"]["Nope"] = {"type": "string"}
    elif change == "broken_template":
        resource["path"] += "{"
    else:
        resource["explode"] = {"array": "ComponentComplianceReports"}

    def read(path, *args, **kwargs):
        if path.name == "catalog.json" and path.parent.name == "dell_ome":
            return json.dumps(catalog)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    _catalog.cache_clear()
    try:
        with pytest.raises(DellOmeError) as error:
            client.get_tables()
        assert error.value.code == "InvalidCatalog"
        assert not http["seen"]
    finally:
        _catalog.cache_clear()


# ----------------------------------------------------------------------------- configuration


@pytest.mark.parametrize(
    "kwargs",
    [
        {"url": "http://ome.example", "allow_http": True},
        {"url": "http://127.0.0.1:18093"},
        {"url": "https://ome.example/api"},
        {"url": "https://user:pw@ome.example"},
        {"url": "https://ome.example", "password": ""},
        {"url": "https://ome.example", "timeout": 0},
        {"url": "https://ome.example", "max_rows": "10"},
    ],
)
def test_unsafe_configuration_is_rejected(kwargs):
    params = {"username": "reader", "password": "test-only", **kwargs}
    with pytest.raises(DellOmeError) as error:
        DellOmeClient(**params)
    assert error.value.code in {"InvalidConfiguration", "InvalidQuery"}


def test_loopback_http_is_allowed_only_with_explicit_simulator_flag():
    assert DellOmeClient(url="http://localhost:18093", username="r", password="p", allow_http=True).url == "http://localhost:18093"


# ----------------------------------------------------------------------------- session handshake


def test_session_handshake_uses_documented_token_header_and_logout(client, http):
    http["queue"].append((200, collection([{"Id": 1, "DeviceName": "a"}])))
    client.execute_query({"table": "devices", "fields": ["Id", "DeviceName"]})
    methods = [(r.method, unquote(urlsplit(r.url).path)) for r in http["seen"]]
    assert methods == [("POST", SESSIONS), ("GET", "/api/DeviceService/Devices"), ("DELETE", f"{SESSIONS}('{LOGIN_BODY['Id']}')")]
    login = http["seen"][0]
    assert json.loads(login.body) == {"UserName": "reader", "Password": "test-only", "SessionType": "API"}
    assert login.headers["Content-Type"] == "application/json"
    assert "X-Auth-Token" not in login.headers
    get = http["seen"][1]
    assert get.headers["X-Auth-Token"] == "tok-123" and "Authorization" not in get.headers
    assert http["seen"][2].headers["X-Auth-Token"] == "tok-123"


def test_logout_runs_even_when_the_query_fails(client, http):
    http["queue"].append((500, {"error": {"message": "boom"}}))
    with pytest.raises(DellOmeError) as error:
        client.execute_query({"table": "devices"})
    assert error.value.code == "HttpError"
    assert [r.method for r in http["seen"]] == ["POST", "GET", "DELETE"]


def test_rejected_credentials_make_no_data_request(client, http):
    http["login"] = (401, {"error": {"code": "Base.1.0.GeneralError", "message": "secret-host-detail"}}, {})
    with pytest.raises(DellOmeError) as error:
        client.execute_query({"table": "devices"})
    assert error.value.code == "AuthenticationFailed"
    assert "secret-host-detail" not in str(error.value)
    assert [r.method for r in http["seen"]] == ["POST"]


def test_session_cap_is_a_distinct_code_with_bounded_message_id(client, http):
    body = {"error": {"code": "Base.1.0.GeneralError", "message": "x", "@Message.ExtendedInfo": [
        {"MessageId": "CUSR1001", "Message": "Unable to create a session because the maximum number of sessions has been reached."}]}}
    http["login"] = (400, body, {})
    with pytest.raises(DellOmeError) as error:
        client.test_connection()
    assert error.value.code == "SessionLimitReached"
    assert error.value.ome_message_id == "CUSR1001"
    assert "maximum number" not in str(error.value)


def test_login_without_token_or_session_id_is_invalid_response(client, http):
    http["login"] = (201, {"Id": "abc"}, {})
    with pytest.raises(DellOmeError) as error:
        client.execute_query({"table": "devices"})
    assert error.value.code == "InvalidResponse"
    assert not gets(http)


# ----------------------------------------------------------------------------- pagination


def test_pagination_follows_nextlink_short_pages_and_reapplies_scope(client, http):
    http["queue"].extend([
        (200, collection([{"Id": 1, "Status": 4000, "ConnectionState": True, "SystemId": 9007199254740993}], count=3,
                         next_link="/api/DeviceService/Devices?$skip=100&$top=100")),
        (200, collection([{"Id": 2, "Status": 4000, "ConnectionState": None}, {"Id": 3, "Status": 4000, "ConnectionState": False}], count=3)),
    ])
    df = client.execute_query({"table": "devices", "fields": ["Id", "Status", "ConnectionState", "SystemId"], "filter": {"Status": 4000}})
    assert list(df["Id"]) == [1, 2, 3]
    assert str(df["Id"].dtype) == "Int64" and str(df["ConnectionState"].dtype) == "boolean"
    assert df.iloc[0]["SystemId"] == 9007199254740993 and pd_isna(df.iloc[1]["ConnectionState"])
    assert df["_bow_query_id"].nunique() == 1 and str(df["_bow_retrieved_at"].dtype) == "datetime64[ns, UTC]"
    first, second = (parse_qs(urlsplit(r.url).query) for r in gets(http))
    assert first["$filter"] == ["Status eq 4000"] and first["$top"] == ["100"] and first["$skip"] == ["0"]
    # OME continuations may drop $filter: the client re-applies the original scope.
    assert second["$filter"] == ["Status eq 4000"] and second["$skip"] == ["100"]


def pd_isna(value):
    import pandas as pd

    return bool(pd.isna(value))


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example/api/DeviceService/Devices?$skip=100&$top=100",
        "/api/AlertService/Alerts?$skip=100&$top=100",
        "/api/DeviceService/Devices?$skip=100&$top=100&$filter=Status eq 1000",
        "/api/DeviceService/Devices?$skip=0&$top=100",
        "/api/DeviceService/Devices?$skip=100&$top=100&$expand=All",
        "/api/DeviceService/Devices?$top=100",
    ],
)
def test_continuations_cannot_change_origin_resource_or_scope(client, http, href):
    http["queue"].append((200, collection([{"Id": 1}], count=2, next_link=href)))
    with pytest.raises(DellOmeError) as error:
        client.execute_query({"table": "devices", "filter": {"Status": 4000}})
    assert error.value.code == "InvalidResponse"
    assert len(gets(http)) == 1


def test_empty_page_with_continuation_is_not_trusted(client, http):
    http["queue"].append((200, collection([], count=5, next_link="/api/DeviceService/Devices?$skip=100&$top=100")))
    with pytest.raises(DellOmeError) as error:
        client.execute_query({"table": "devices"})
    assert error.value.code == "InvalidResponse"


def test_odata_count_above_limit_fails_before_fetching_more_pages(client, http):
    http["queue"].append((200, collection([{"Id": 1}], count=500, next_link="/api/DeviceService/Devices?$skip=1&$top=1")))
    with pytest.raises(DellOmeError) as error:
        client.execute_query({"table": "devices", "limit": 10})
    assert error.value.code == "ResultLimitExceeded"
    assert len(gets(http)) == 1


def test_limit_never_silently_truncates_without_count(client, http):
    body1 = {"value": [{"Id": 1}, {"Id": 2}], "@odata.nextLink": "/api/DeviceService/Devices?$skip=2&$top=2"}
    body2 = {"value": [{"Id": 3}]}
    http["queue"].extend([(200, body1), (200, body2)])
    with pytest.raises(DellOmeError) as error:
        client.execute_query({"table": "devices", "limit": 2})
    assert error.value.code == "ResultLimitExceeded"


def test_top_is_bounded_by_limit_and_singletons_send_no_controls(client, http):
    http["queue"].append((200, collection([])))
    client.execute_query({"table": "devices", "limit": 7})
    assert parse_qs(urlsplit(gets(http)[0].url).query)["$top"] == ["7"]
    http["queue"].append((200, {"Name": "OME", "Version": "4.1.0"}))
    df = client.execute_query({"table": "appliance_info"})
    assert not urlsplit(gets(http)[1].url).query
    assert df.iloc[0]["Version"] == "4.1.0"


# ----------------------------------------------------------------------------- errors


@pytest.mark.parametrize(
    "status,code",
    [(400, "InvalidRequest"), (401, "AuthenticationFailed"), (403, "PermissionDenied"), (404, "UnsupportedResource"),
     (405, "ForbiddenOperation"), (418, "HttpError")],
)
def test_http_errors_map_to_codes_without_body_leakage(client, http, status, code):
    body = {"error": {"code": "Base.1.0.GeneralError", "message": "sensitive customer text", "@Message.ExtendedInfo": [
        {"MessageId": "CGEN1008", "Message": "sensitive customer text"}]}}
    http["queue"].append((status, body))
    with pytest.raises(DellOmeError) as error:
        client.execute_query({"table": "alerts"})
    assert error.value.code == code and error.value.status == status
    assert error.value.ome_message_id == "CGEN1008"
    assert "sensitive" not in str(error.value)
    assert client.get_schema("alerts").metadata_json["dell_ome"]["availability"] != "unverified"


def test_transient_errors_retry_then_return_complete_data(client, http, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    http["queue"].extend([(503, {}), (200, collection([{"Id": 7}]))])
    df = client.execute_query({"table": "devices", "fields": ["Id"]})
    assert list(df["Id"]) == [7] and len(gets(http)) == 2


def test_odata_error_envelope_with_200_is_not_data(client, http):
    http["queue"].append((200, {"error": {"code": "Base.1.0.GeneralError"}}))
    with pytest.raises(DellOmeError) as error:
        client.execute_query({"table": "devices"})
    assert error.value.code == "InvalidResponse"


# ----------------------------------------------------------------------------- validation (no HTTP)


@pytest.mark.parametrize(
    "query",
    [
        {"table": "nope"},
        {"table": "devices", "path": "/api/x"},
        {"table": "devices", "fields": ["Password"]},
        {"table": "devices", "fields": []},
        {"table": "devices", "filter": {"DeviceManagement": "x"}},
        {"table": "devices", "filter": {"Status": "4000"}},
        {"table": "devices", "filter": {"Status": {"op": "like", "value": 1}}},
        {"table": "devices", "filter": {"Status": {"op": "contains", "value": "4"}}},
        {"table": "devices", "filter": {"Status": {"op": "in", "value": []}}},
        {"table": "devices", "filter": {"DeviceName": "bad\nname"}},
        {"table": "devices", "limit": 0},
        {"table": "devices", "limit": True},
        {"table": "devices", "lookback": "24h"},
        {"table": "alerts", "lookback": "24x"},
        {"table": "alerts", "lookback": "2d", "filter": {"TimeStamp": "2024-01-01"}},
        {"table": "jobs", "order_by": ["Id desc"]},
        {"table": "devices", "order_by": ["Targets asc"]},
        {"table": "devices", "order_by": ["Id sideways"]},
        {"table": "device_inventory"},
        {"table": "device_inventory", "parent": {"DeviceId": "../../x"}},
        {"table": "device_inventory", "parent": {"DeviceId": -1}},
        {"table": "device_inventory", "parent": {"DeviceId": 1, "InventoryType": "server Processors"}},
        {"table": "device_inventory", "parent": {"DeviceId": 1, "Extra": 2}},
        {"table": "device_inventory", "parent": {"DeviceId": 1}, "filter": {"InventoryType": "x"}},
        {"table": "devices", "parent": {"DeviceId": 1}},
        {"table": "job_execution_details", "parent": {"JobId": 1}},
    ],
)
def test_invalid_or_unsafe_queries_make_no_http_request(client, http, query):
    with pytest.raises(DellOmeError) as error:
        client.execute_query(query)
    assert error.value.code == "InvalidQuery"
    assert not http["seen"]


def test_query_must_be_a_json_object(client, http):
    for bad in ["SELECT * FROM devices", 42, None, "{not json"]:
        with pytest.raises(DellOmeError) as error:
            client.execute_query(bad)
        assert error.value.code == "InvalidQuery"
    assert not http["seen"]


def test_mutating_or_unlisted_requests_are_refused_before_sending(client, http):
    """The allow-list is the security boundary: only catalog GETs and the session handshake may leave the client."""
    with requests.Session() as session:
        for method, path in [("POST", "/api/DeviceService/Devices"), ("DELETE", "/api/DeviceService/Devices(1)"),
                             ("PUT", SESSIONS), ("PATCH", "/api/GroupService/Groups(1)"),
                             ("POST", "/api/JobService/Actions/JobService.RunJobs"), ("GET", "/api/AccountService/Accounts"),
                             ("GET", "/api/DeviceService/Devices(1)/Actions/DeviceService.Reset")]:
            with pytest.raises(DellOmeError) as error:
                client._send(session, method, path, None, 10**12)
            assert error.value.code == "ForbiddenOperation", (method, path)
    assert not http["seen"]


# ----------------------------------------------------------------------------- rendering


def test_filters_render_as_odata_with_escaping_and_operators(client, http):
    http["queue"].append((200, collection([])))
    client.execute_query({"table": "alerts", "filter": {
        "AlertDeviceName": "O'Brien's host",
        "SeverityType": {"op": "ge", "value": 8},
        "StatusType": {"op": "in", "value": [1000, 2000]},
        "Message": {"op": "contains", "value": "power"},
    }, "order_by": ["TimeStamp desc", "Id"]})
    params = parse_qs(urlsplit(gets(http)[0].url).query)
    assert params["$filter"] == ["AlertDeviceName eq 'O''Brien''s host' and SeverityType ge 8 and (StatusType eq 1000 or StatusType eq 2000) and contains(Message,'power')"]
    assert params["$orderby"] == ["TimeStamp desc,Id"]
    http["queue"].append((200, collection([], entity="JobService.Job")))
    client.execute_query({"table": "jobs", "filter": {"LastRunStatus.Id": 2070, "JobType.Internal": False}})
    assert parse_qs(urlsplit(gets(http)[1].url).query)["$filter"] == ["LastRunStatus/Id eq 2070 and JobType/Internal eq false"]


@pytest.mark.parametrize("table,field", [("alerts", "TimeStamp"), ("audit_logs", "CreatedDate")])
def test_lookback_renders_a_documented_timestamp_lower_bound(client, http, table, field):
    http["queue"].append((200, collection([])))
    client.execute_query({"table": table, "lookback": "36h"})
    flt = parse_qs(urlsplit(gets(http)[0].url).query)["$filter"][0]
    assert re.fullmatch(field + r" ge '\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.000'", flt)


def test_optional_inventory_type_selects_the_singleton_variant(client, http):
    http["queue"].append((200, {"InventoryType": "serverPowerSupplies", "InventoryInfo": [
        {"Id": 3000, "Name": "PS1 Status", "Status": 1000}, {"Id": 3001, "Name": "PS2 Status", "Status": 4000, "password": "nope"}]}))
    df = client.execute_query({"table": "device_inventory", "parent": {"DeviceId": 10002, "InventoryType": "serverPowerSupplies"}})
    assert unquote(urlsplit(gets(http)[0].url).path) == "/api/DeviceService/Devices(10002)/InventoryDetails('serverPowerSupplies')"
    assert not urlsplit(gets(http)[0].url).query
    assert len(df) == 2 and list(df["InventoryType"]) == ["serverPowerSupplies", "serverPowerSupplies"]
    assert df.iloc[1]["InventoryItem"] == {"Id": 3001, "Name": "PS2 Status", "Status": 4000}
    assert list(df["DeviceId"]) == [10002, 10002] and str(df["DeviceId"].dtype) == "Int64"
    http["queue"].append((200, collection([{"InventoryType": "serverProcessors", "InventoryInfo": [{"Id": 1}, {"Id": 2}]},
                                           {"InventoryType": "serverMemoryDevices", "InventoryInfo": []}], entity="DeviceService.InventoryDetail")))
    df = client.execute_query({"table": "device_inventory", "parent": {"DeviceId": 10002}})
    assert unquote(urlsplit(gets(http)[1].url).path) == "/api/DeviceService/Devices(10002)/InventoryDetails"
    assert len(df) == 3 and pd_isna(df.iloc[2]["InventoryItem"])


def test_compliance_reports_flatten_per_component_and_keep_componentless_devices(client, http):
    http["queue"].append((200, collection([
        {"Id": 194, "DeviceId": 11603, "ServiceTag": "MXL1234", "ComplianceStatus": "CRITICAL", "RebootRequired": True,
         "ComponentComplianceReports": [
             {"Id": 1258, "Name": "BIOS", "CurrentVersion": "1.12.1", "Version": "1.14.2", "ComplianceStatus": "CRITICAL", "RebootRequired": True},
             {"Id": 1259, "Name": "iDRAC", "CurrentVersion": "7.00.60.00", "Version": "7.00.60.00", "ComplianceStatus": "OK", "RebootRequired": False}]},
        {"Id": 195, "DeviceId": 11604, "ServiceTag": "MXL9999", "ComplianceStatus": "OK", "ComponentComplianceReports": []},
    ], entity="UpdateService.DeviceComplianceReport")))
    df = client.execute_query({"table": "firmware_compliance_reports", "parent": {"BaselineId": 50}})
    assert unquote(urlsplit(gets(http)[0].url).path) == "/api/UpdateService/Baselines(50)/DeviceComplianceReports"
    assert len(df) == 3 and list(df["DeviceId"]) == [11603, 11603, 11604]
    assert list(df["Component.Name"].fillna("-")) == ["BIOS", "iDRAC", "-"]
    assert str(df["Component.RebootRequired"].dtype) == "boolean" and str(df["Component.Id"].dtype) == "Int64"
    assert set(df["BaselineId"]) == {50}


def test_alert_categories_flatten_with_prefixed_columns(client, http):
    http["queue"].append((200, collection([{"Name": "iDRAC", "IsBuiltIn": True, "CategoriesDetails": [
        {"Id": 1, "Name": "System Health", "CatalogName": "iDRAC", "SubCategoryDetails": [{"Id": 50, "Name": "Fan"}]},
        {"Id": 2, "Name": "Storage", "CatalogName": "iDRAC", "SubCategoryDetails": []}]}], entity="AlertService.AlertCategory")))
    df = client.execute_query({"table": "alert_categories"})
    assert list(df["Category.Id"]) == [1, 2] and df.iloc[0]["Category.SubCategoryDetails"] == [{"Id": 50, "Name": "Fan"}]


def test_secret_like_values_and_odata_annotations_are_removed(client, http):
    http["queue"].append((200, collection([{
        "Id": 1, "JobName": "disc", "@odata.id": "/api/JobService/Jobs(1)",
        "Params": [{"JobId": 1, "Key": "discoveryTargets", "Value": "192.0.2.1"}, {"JobId": 1, "Key": "password", "Value": "s3cret"},
                   {"JobId": 1, "Key": "authToken", "Value": "s3cret"}],
        "Targets": [{"Id": 5, "Data": "", "TargetType": {"Id": 1000, "Name": "DEVICE"}, "secret_key": "s3cret", "Nested@odata.count": 1}],
    }], entity="JobService.Job")))
    df = client.execute_query({"table": "jobs", "fields": ["Id", "Params", "Targets"]})
    dumped = df.to_json()
    assert "s3cret" not in dumped and "@odata" not in dumped
    assert df.iloc[0]["Params"][0]["Value"] == "192.0.2.1" and df.iloc[0]["Params"][1]["Value"] is None
    assert df.iloc[0]["Targets"][0]["TargetType"] == {"Id": 1000, "Name": "DEVICE"}
    http["queue"].append((200, collection([{"Id": 22, "Repository": {"Name": "r", "Username": "svc", "Password": "s3cret"}}], entity="UpdateService.Catalogs")))
    df = client.execute_query({"table": "firmware_catalogs", "fields": ["Repository"]})
    assert df.iloc[0]["Repository.Username"] == "svc" and "s3cret" not in df.to_json()


def test_invalid_value_types_are_typed_errors_not_coerced(client, http):
    http["queue"].append((200, collection([{"Id": "not-an-int"}])))
    with pytest.raises(DellOmeError) as error:
        client.execute_query({"table": "devices", "fields": ["Id"]})
    assert error.value.code == "InvalidResponse"


def test_every_catalog_table_builds_a_bounded_allow_listed_request(client, http):
    for table in client.get_tables():
        meta = table.metadata_json["dell_ome"]
        parents = {k: 7 for k in meta["parents"]}
        http["queue"].append((200, collection([]) if meta["collection"] else {}))
        frame = client.execute_query({"table": table.name, "parent": parents} if parents else {"table": table.name})
        request = gets(http)[-1]
        expected = meta["endpoint"]
        for k, v in parents.items():
            expected = expected.replace("{" + k + "}", str(v))
        assert unquote(urlsplit(request.url).path) == expected, table.name
        assert set(parse_qs(urlsplit(request.url).query)) <= set(meta["controls"]), table.name
        assert str(frame["_bow_retrieved_at"].dtype) == "datetime64[ns, UTC]"
        assert set(_catalog()["resources"][table.name]["fields"]) <= set(frame.columns)
    assert len([r for r in http["seen"] if r.method == "DELETE"]) == len(_catalog()["resources"])


# ----------------------------------------------------------------------------- connection test


def test_connection_reports_version_and_table_count(client, http):
    http["queue"].append((200, {"Name": "OpenManage Enterprise", "Version": "4.1.0", "Guid": "7d1b2e2c"}))
    result = client.test_connection()
    assert result["success"] and "4.1.0" in result["message"] and str(len(_catalog()["resources"])) in result["message"]
    assert unquote(urlsplit(gets(http)[0].url).path) == "/api/ApplicationService/Info"
    http["queue"].append((200, collection([{"Id": 1}])))
    assert set(client.execute_query({"table": "devices"})["_bow_appliance"]) == {"7d1b2e2c"}


@pytest.mark.parametrize("body,code", [({"Name": "OME"}, "InvalidResponse"), ({"Version": "2.0.0"}, "UnsupportedVersion"),
                                       ({"Version": "latest"}, "InvalidResponse")])
def test_connection_is_not_reported_without_a_supported_version(client, http, body, code):
    http["queue"].append((200, body))
    with pytest.raises(DellOmeError) as error:
        client.test_connection()
    assert error.value.code == code


def test_query_alias_and_async_wrapper_use_same_contract(client, http):
    import asyncio

    http["queue"].extend([(200, collection([])), (200, collection([]))])
    assert client.query({"table": "devices"}).empty
    assert asyncio.run(client.aexecute_query({"table": "devices"})).empty
    assert len(gets(http)) == 2


def test_fleet_query_runs_through_bow_code_executor(client, http):
    """Mirrors the NetApp reference: structured queries are captured as evidence by the sandboxed executor."""
    from app.ai.code_execution.code_execution import StreamingCodeExecutor

    http["queue"].append((200, collection([{"Id": 1, "DeviceName": "a", "Status": 4000}, {"Id": 2, "DeviceName": "b", "Status": 1000}])))
    code = """
def generate_df(ds_clients, excel_files):
    df = ds_clients["ome"].execute_query({"table": "devices", "fields": ["Id", "DeviceName", "Status"]})
    return df[df["Status"] == 4000]
"""
    client._bow_connection_id = "synthetic-connection"
    frame, _, queries = StreamingCodeExecutor().execute_code(code=code, ds_clients={"ome": client}, excel_files=[])
    assert list(frame["DeviceName"]) == ["a"] and set(frame["_bow_connection"]) == {"synthetic-connection"}
    assert len(queries) == 1 and json.loads(queries[0])["table"] == "devices"


# ----------------------------------------------------------------------------- simulator


def test_real_client_against_in_process_simulator():
    sys.path.insert(0, str(Path(__file__).parents[3] / "tools/dell_ome"))
    import simulated_api as sim

    server = sim.build_server(0, "svc-bow", "synthetic-only", max_sessions=3)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        client = DellOmeClient(url=base, username="svc-bow", password="synthetic-only", allow_http=True)
        assert client.test_connection()["success"]
        for table in client.get_tables():
            meta = table.metadata_json["dell_ome"]
            parents = {"DeviceId": 10002, "BaselineId": 50 if table.name == "firmware_compliance_reports" else 60,
                       "JobId": 10004, "ExecutionHistoryId": 1000400, "GroupId": 10101}
            query = {"table": table.name}
            if meta["parents"]:
                query["parent"] = {k: parents[k] for k in meta["parents"]}
            frame = client.execute_query(query)
            assert len(frame) > 0, table.name
        critical = client.execute_query({"table": "devices", "filter": {"Status": 4000}, "order_by": ["Id desc"]})
        assert len(critical) > 0 and set(critical["Status"]) == {4000} and list(critical["Id"]) == sorted(critical["Id"], reverse=True)
        paged = client.execute_query({"table": "alerts", "filter": {"SeverityType": {"op": "ge", "value": 8}}, "lookback": "7d", "limit": 1000})
        assert len(paged) > 100 and paged["SeverityType"].min() >= 8
        bad = DellOmeClient(url=base, username="svc-bow", password="wrong", allow_http=True)
        with pytest.raises(DellOmeError) as error:
            bad.test_connection()
        assert error.value.code == "AuthenticationFailed"
        with pytest.raises(DellOmeError) as error:
            client.execute_query({"table": "device_power", "parent": {"DeviceId": 99999}})
        assert error.value.code == "UnsupportedResource"
        # The simulator refuses mutations even to an authenticated caller, like the Viewer role on the appliance.
        raw = requests.Session()
        raw.trust_env = False
        login = raw.post(f"{base}{SESSIONS}", json={"UserName": "svc-bow", "Password": "synthetic-only", "SessionType": "API"})
        assert login.status_code == 201 and login.headers["X-Auth-Token"]
        headers = {"X-Auth-Token": login.headers["X-Auth-Token"]}
        assert raw.post(f"{base}/api/DeviceService/Devices", json={}, headers=headers).status_code == 405
        assert raw.post(f"{base}/api/JobService/Actions/JobService.RunJobs", json={}, headers=headers).status_code == 403
        assert raw.get(f"{base}/api/DeviceService/Devices").status_code == 401
        assert raw.delete(f"{base}{SESSIONS}('{login.json()['Id']}')", headers=headers).status_code == 204
        assert not server.RequestHandlerClass.sessions.list()
    finally:
        server.shutdown()
        server.server_close()
