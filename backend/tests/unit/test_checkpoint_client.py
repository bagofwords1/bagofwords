"""Check Point Management API contracts exercised at the HTTP boundary; no appliance credentials."""

import io
import json
import threading
from urllib.parse import urlsplit

import pandas as pd
import pytest
import requests

from app.data_sources.clients.checkpoint_client import CheckPointClient, CheckPointError
from app.schemas.data_source_registry import resolve_client_class

PRIMARY = [
    "hosts", "networks", "address_ranges", "groups", "group_members", "services_tcp", "services_udp", "service_groups",
    "packages", "access_layers", "access_rules", "nat_rules", "gateways_and_servers", "simple_gateways", "sessions",
    "unused_objects", "tasks", "logs", "audit_logs", "api_versions", "domains", "vpn_communities", "changes", "session",
]
LOGIN_REPLY = {
    "sid": "97BVpRfN4j81ogN-V2XqGYmw3DDwIhoSn0og8PiKDiM", "url": "https://mgmt.example:443/web_api",
    "uid": "7a13a360-9b24-40d7-acd3-5b50247be33e", "session-timeout": 600, "api-server-version": "1.9.1", "read-only": True,
    "last-login-was-at": {"posix": 1430032266851, "iso-8601": "2015-04-26T10:11+0300"},
}


def page(objects, offset=0, total=None, container="objects"):
    total = len(objects) if total is None else total
    return {"from": offset + 1 if objects else 0, "to": offset + len(objects), "total": total, container: objects}


def obj(name, kind="host", **fields):
    return {
        "uid": f"uid-{name}", "name": name, "type": kind,
        "domain": {"uid": "41e821a0-3720-11e3-aa6e-0800200c9fde", "name": "SMC User", "domain-type": "domain"},
        "meta-info": {"lock": "unlocked", "validation-state": "ok", "last-modifier": "aa", "creator": "aa",
                      "last-modify-time": {"posix": 1445504073840, "iso-8601": "2015-10-22T11:54+0300"},
                      "creation-time": {"posix": 1445245489542, "iso-8601": "2015-10-19T12:04+0300"}},
        "tags": [], "read-only": False, "comments": "", "color": "black", "icon": "Objects/host", "groups": [], **fields,
    }


def test_registry_resolves_the_read_only_catalog():
    cls = resolve_client_class("checkpoint")
    client = cls(url="https://mgmt.example", username="reader", password="test-only")
    tables = {t.name: t for t in client.get_tables()}
    assert set(PRIMARY) <= tables.keys()
    assert tables["access_rules"].metadata_json["checkpoint"]["parents"] == ["layer"]
    assert tables["nat_rules"].metadata_json["checkpoint"]["parents"] == ["package"]
    assert all(c.startswith("show-") or c in {"login", "logout", "keepalive"} for c in client.allowed_commands())
    assert not any(c.startswith(("add-", "set-", "delete-", "install", "publish", "discard", "run-")) for c in client.allowed_commands())
    assert len(client.prompt_schema().splitlines()) == len(tables)
    client.close()


@pytest.fixture
def client():
    c = CheckPointClient(url="https://mgmt.example", username="reader", password="test-only")
    yield c
    c.close()


@pytest.fixture
def http(monkeypatch):
    """Serve login/logout automatically; every other command pops (status, body) from the queue."""
    queue, seen = [], []

    def send(session, request, **kwargs):
        seen.append(request)
        assert request.method == "POST"
        assert request.headers["Content-Type"] == "application/json"
        command = urlsplit(request.url).path.rsplit("/", 1)[-1]
        if command == "login":
            status, body = 200, LOGIN_REPLY
        elif command == "logout":
            status, body = 200, {"message": "OK"}
        else:
            status, body = queue.pop(0)
        r = requests.Response()
        r.status_code = status
        r.raw = io.BytesIO(json.dumps(body).encode())
        r.url = request.url
        return r

    monkeypatch.setattr(requests.Session, "send", send)
    return queue, seen


def commands(seen):
    return [urlsplit(r.url).path.rsplit("/", 1)[-1] for r in seen]


def body_of(request):
    return json.loads(request.body)


def test_login_is_read_only_and_session_header_is_used_until_logout(client, http):
    http[0].append((200, page([obj("h1", **{"ipv4-address": "10.0.0.1"})])))
    frame = client.execute_query({"table": "hosts", "fields": ["name", "ipv4-address"]})
    assert commands(http[1]) == ["login", "show-hosts", "logout"]
    login = http[1][0]
    assert body_of(login) == {"user": "reader", "password": "test-only", "read-only": True}
    assert "X-chkp-sid" not in login.headers and "continue-last-session" not in body_of(login)
    assert all(r.headers["X-chkp-sid"] == LOGIN_REPLY["sid"] for r in http[1][1:])
    assert urlsplit(login.url).path == "/web_api/login"
    assert frame.iloc[0]["ipv4-address"] == "10.0.0.1"
    assert frame.iloc[0]["_bow_api_version"] == "1.9.1"


def test_logout_runs_even_when_the_query_fails(client, http):
    http[0].append((500, {"code": "generic_internal_error", "message": "boom"}))
    with pytest.raises(CheckPointError) as e:
        client.execute_query({"table": "hosts"})
    assert e.value.code == "RemoteError" and "boom" not in str(e.value)
    assert commands(http[1]) == ["login", "show-hosts", "logout"]


def test_api_key_login_domain_and_pinned_version_prefix(http):
    c = CheckPointClient(
        url="https://tenant.maas.checkpoint.com/abc123/web_api", api_key="k3y", domain="Dom1", api_version="1.9.1"
    )
    http[0].append((200, page([])))
    c.execute_query({"table": "networks"})
    assert body_of(http[1][0]) == {"api-key": "k3y", "read-only": True, "domain": "Dom1"}
    assert urlsplit(http[1][0].url).path == "/abc123/web_api/v1.9.1/login"
    assert urlsplit(http[1][1].url).path == "/abc123/web_api/v1.9.1/show-networks"
    c.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"url": "http://mgmt.example", "username": "a", "password": "b", "allow_http": True},
        {"url": "https://mgmt.example", "username": "a", "password": "b", "api_key": "k"},
        {"url": "https://mgmt.example"},
        {"url": "https://mgmt.example", "username": "a"},
        {"url": "https://user:pw@mgmt.example", "username": "a", "password": "b"},
        {"url": "https://mgmt.example/other/path", "username": "a", "password": "b"},
        {"url": "https://mgmt.example", "username": "a", "password": "b", "api_version": "v1.9"},
    ],
)
def test_invalid_configuration_is_rejected_before_any_request(kwargs, http):
    with pytest.raises(CheckPointError) as e:
        CheckPointClient(**kwargs)
    assert e.value.code == "InvalidConfiguration"
    assert not http[1]


def test_collection_pages_follow_to_offset_until_total(client, http):
    http[0].extend(
        [
            (200, page([obj(f"h{i}") for i in range(500)], 0, 1001)),
            (200, page([obj(f"h{i}") for i in range(500, 1000)], 500, 1001)),
            (200, page([obj("h1000", **{"ipv4-address": "10.9.9.9"})], 1000, 1001)),
        ]
    )
    frame = client.execute_query({"table": "hosts", "fields": ["name", "ipv4-address"]})
    assert len(frame) == 1001
    bodies = [body_of(r) for r in http[1][1:-1]]
    assert [b["offset"] for b in bodies] == [0, 500, 1000]
    assert all(b["limit"] == 500 and b["details-level"] == "full" for b in bodies)
    assert frame["_bow_query_id"].nunique() == 1


def test_empty_collection_has_schema_and_no_second_page(client, http):
    http[0].append((200, {"from": 0, "to": 0, "total": 0, "objects": []}))
    frame = client.execute_query({"table": "hosts", "fields": ["name", "nat-settings.auto-rule"]})
    assert frame.empty and str(frame["nat-settings.auto-rule"].dtype) == "boolean"
    assert commands(http[1]) == ["login", "show-hosts", "logout"]


@pytest.mark.parametrize(
    "reply",
    [
        {"from": 1, "to": 1, "total": 5, "objects": []},  # claims more but returns nothing
        {"from": 1, "to": 0, "total": 5, "objects": [{"uid": "a"}]},  # cursor does not advance
        {"from": 1, "to": 1, "objects": [{"uid": "a"}]},  # missing total
        {"from": 1, "to": 1, "total": 1, "objects": ["uid-only"]},  # wrong details level
    ],
)
def test_broken_paging_envelopes_are_refused_not_truncated(client, http, reply):
    http[0].append((200, reply))
    with pytest.raises(CheckPointError) as e:
        client.execute_query({"table": "hosts"})
    assert e.value.code == "InvalidResponse"
    assert commands(http[1]) == ["login", "show-hosts", "logout"]


def test_complete_result_limit_never_silently_truncates(client, http):
    http[0].append((200, page([obj("a"), obj("b")])))
    with pytest.raises(CheckPointError) as e:
        client.execute_query({"table": "hosts", "limit": 1})
    assert e.value.code == "ResultLimitExceeded"


def test_server_total_above_ceiling_stops_after_first_page(http):
    c = CheckPointClient(url="https://mgmt.example", username="r", password="p", max_rows=10)
    http[0].append((200, page([obj(f"h{i}") for i in range(10)], 0, 5000)))
    with pytest.raises(CheckPointError) as e:
        c.execute_query({"table": "hosts"})
    assert e.value.code == "ResultLimitExceeded"
    assert commands(http[1]) == ["login", "show-hosts", "logout"]
    c.close()


@pytest.mark.parametrize(
    "status,body,code",
    [
        (401, {"code": "generic_err_wrong_session_id", "message": "Wrong session id [secret-sid]"}, "AuthenticationFailed"),
        (400, {"code": "generic_err_missing_session_id", "message": "Missing header: [X-chkp-sid] customer text"}, "AuthenticationFailed"),
        (403, {"code": "generic_err_no_permissions", "message": "customer text"}, "PermissionDenied"),
        (404, {"code": "generic_err_command_not_found", "message": "Command [show-hosts] not found"}, "UnsupportedCommand"),
        (404, {"code": "generic_err_object_not_found", "message": "Requested object [Secret Layer] not found"}, "UnsupportedResource"),
        (400, {"code": "generic_err_invalid_parameter", "message": "customer text"}, "InvalidRequest"),
        (429, {"code": "err_too_many_requests", "message": "Too many requests"}, "Throttled"),
        (302, {}, "RemoteError"),
    ],
)
def test_http_failures_map_to_codes_without_leaking_bodies(client, http, status, body, code, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    http[0].extend([(status, body)] * 4)
    with pytest.raises(CheckPointError) as e:
        client.execute_query({"table": "hosts"})
    assert e.value.code == code
    assert "customer" not in str(e.value) and "secret" not in str(e.value).lower()
    assert e.value.vendor_code == body.get("code")


def test_login_failure_is_authentication_failed(monkeypatch, client):
    seen = []

    def send(session, request, **kwargs):
        seen.append(request)
        r = requests.Response()
        r.status_code = 400
        r.raw = io.BytesIO(json.dumps({"code": "err_login_failed", "message": "Authentication to server failed."}).encode())
        return r

    monkeypatch.setattr(requests.Session, "send", send)
    with pytest.raises(CheckPointError) as e:
        client.execute_query({"table": "hosts"})
    assert e.value.code == "AuthenticationFailed" and e.value.vendor_code == "err_login_failed"
    assert commands(seen) == ["login"]


def test_read_write_session_is_refused(monkeypatch, client):
    seen = []

    def send(session, request, **kwargs):
        seen.append(request)
        command = urlsplit(request.url).path.rsplit("/", 1)[-1]
        r = requests.Response()
        r.status_code = 200
        body = {**LOGIN_REPLY, "read-only": False} if command == "login" else {"message": "OK"}
        r.raw = io.BytesIO(json.dumps(body).encode())
        return r

    monkeypatch.setattr(requests.Session, "send", send)
    with pytest.raises(CheckPointError) as e:
        client.execute_query({"table": "hosts"})
    assert e.value.code == "ReadOnlyRequired"
    assert commands(seen) == ["login"]


@pytest.mark.parametrize(
    "query",
    [
        {"table": "no_such_table"},
        {"table": "hosts", "fields": ["password"]},
        {"table": "hosts", "fields": []},
        {"table": "hosts", "parent": {"layer": "x"}},
        {"table": "access_rules"},
        {"table": "access_rules", "parent": {"layer": "x", "package": "y"}},
        {"table": "access_rules", "parent": {"layer": "bad\nname"}},
        {"table": "hosts", "limit": True},
        {"table": "hosts", "limit": 0},
        {"table": "hosts", "filter": {"name": {"op": "regex", "value": "x"}}},
        {"table": "hosts", "filter": {"nope": "x"}},
        {"table": "hosts", "filter": {"name": {"op": "in", "value": "x"}}},
        {"table": "hosts", "lookback": "24h"},
        {"table": "logs", "lookback": "yesterday"},
        {"table": "logs", "lookback": "24h", "time_frame": "today"},
        {"table": "logs", "time_frame": "last-year"},
        {"table": "logs", "end_time": "2026-01-01T00:00:00Z"},
        {"table": "logs", "start_time": "2999-01-01T00:00:00Z"},
        {"table": "logs", "fields": ["_bow_secret"]},
        {"table": "hosts", "order_by": ["ipv4-address asc"]},
        {"table": "tasks", "status": "done"},
        {"table": "hosts", "command": "add-host"},
        {"table": "hosts", "search": "a" * 1001},
        {"table": "changes", "limit_sessions": 0},
        "not json at all",
        42,
    ],
)
def test_invalid_queries_make_no_http_request(client, http, query):
    with pytest.raises(CheckPointError) as e:
        client.execute_query(query)
    assert e.value.code == "InvalidQuery"
    assert not http[1]


@pytest.mark.parametrize("command", ["publish", "add-host", "set-access-rule", "delete-host", "install-policy", "run-script", "discard"])
def test_mutating_commands_cannot_be_sent(client, http, command):
    with pytest.raises(CheckPointError) as e:
        client._post(command, {}, "sid", 10**12)
    assert e.value.code == "ForbiddenOperation"
    assert not http[1]


def test_catalog_with_a_mutating_command_is_rejected(client, http, monkeypatch):
    from pathlib import Path

    from app.data_sources.clients.checkpoint_client import _catalog

    original = Path.read_text
    catalog = json.loads(original(Path(__file__).parents[2] / "app/data_sources/clients/checkpoint/catalog.json"))
    catalog["allowed_commands"].append("add-host")

    def read(path, *args, **kwargs):
        if path.name == "catalog.json" and path.parent.name == "checkpoint":
            return json.dumps(catalog)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    _catalog.cache_clear()
    try:
        with pytest.raises(CheckPointError) as e:
            client.get_tables()
        assert e.value.code == "InvalidCatalog" and not http[1]
    finally:
        _catalog.cache_clear()


def test_secret_like_keys_are_stripped_from_nested_values(client, http):
    community = obj(
        "partner-star", "vpn-community-star",
        **{"use-shared-secret": True, "shared-secrets": [{"external-gateway": "peer", "shared-secret": "s3cr3t-value"}],
           "encryption-method": "ikev2 only", "center-gateways": [{"uid": "g", "name": "gw", "password": "leak"}]},
    )
    http[0].extend([(200, page([])), (200, page([community]))])
    frame = client.execute_query({"table": "vpn_communities", "fields": ["name", "community-type", "use-shared-secret", "center-gateways"]})
    dumped = frame.to_json()
    assert "s3cr3t" not in dumped and "leak" not in dumped and "shared-secrets" not in frame.columns
    with pytest.raises(CheckPointError):  # secret-bearing keys are not even addressable as columns
        client.execute_query({"table": "vpn_communities", "fields": ["shared-secrets"]})
    assert bool(frame.iloc[0]["use-shared-secret"]) is True
    assert frame.iloc[0]["community-type"] == "star"
    assert frame.iloc[0]["center-gateways"][0]["name"] == "gw"


def rulebase_reply():
    return {
        "from": 1, "to": 3, "total": 3, "name": "Network", "uid": "layer-uid",
        "rulebase": [
            {"uid": "sec-1", "name": "Section1", "type": "access-section", "from": 1, "to": 2, "rulebase": [
                {"uid": "r1", "name": "Rule1", "type": "access-rule", "enabled": True, "rule-number": 1,
                 "source": ["h-1"], "source-negate": False, "destination": ["any"], "service": ["svc-1", "unknown-uid"],
                 "action": "act-accept", "track": {"type": "trk-log", "alert": "none"}, "install-on": ["pt"],
                 "comments": "", "custom-fields": {"field-1": "x"}, "hits": {"value": 12, "level": "low"},
                 "meta-info": {"last-modifier": "aa", "last-modify-time": {"posix": 1, "iso-8601": "2015-10-22T11:54+0300"}}},
                {"uid": "r2", "name": "Disabled", "type": "access-rule", "enabled": False, "rule-number": 2,
                 "source": ["any"], "destination": ["any"], "service": ["any"], "action": "act-drop", "track": "trk-none",
                 "track-alert": "none", "install-on": ["pt"]},
            ]},
            {"uid": "r3", "name": "Cleanup rule", "type": "access-rule", "enabled": True, "rule-number": 3,
             "source": ["any"], "destination": ["any"], "service": ["any"], "action": "act-drop", "track": "trk-log", "install-on": ["pt"]},
        ],
        "objects-dictionary": [
            {"uid": "h-1", "name": "web-01", "type": "host"}, {"uid": "any", "name": "Any", "type": "CpmiAnyObject"},
            {"uid": "svc-1", "name": "https", "type": "service-tcp"}, {"uid": "act-accept", "name": "Accept", "type": "RulebaseAction"},
            {"uid": "act-drop", "name": "Drop", "type": "RulebaseAction"}, {"uid": "trk-log", "name": "Log", "type": "Track"},
            {"uid": "trk-none", "name": "None", "type": "Track"}, {"uid": "pt", "name": "Policy Targets", "type": "Global"},
        ],
    }


def test_access_rulebase_is_flattened_and_resolved_through_the_dictionary(client, http):
    http[0].append((200, rulebase_reply()))
    frame = client.execute_query({"table": "access_rules", "parent": {"layer": "Network"}, "search": "web-01", "show_hits": True})
    body = body_of(http[1][1])
    assert body["name"] == "Network" and body["use-object-dictionary"] is True and body["show-as-ranges"] is False
    assert body["details-level"] == "standard" and body["filter"] == "web-01" and body["show-hits"] is True
    assert list(frame["rule-number"]) == [1, 2, 3]
    assert [None if pd.isna(x) else x for x in frame["section"]] == ["Section1", "Section1", None]
    assert list(frame["action"]) == ["Accept", "Drop", "Drop"]
    assert list(frame["track"]) == ["Log", "None", "Log"]
    assert frame.iloc[0]["source"] == ["web-01"] and frame.iloc[0]["source.types"] == ["host"]
    assert frame.iloc[0]["service"] == ["https", "unknown-uid"]  # unresolved uids stay visible, never invented
    assert frame.iloc[0]["install-on"] == ["Policy Targets"]
    assert frame.iloc[0]["hits.value"] == 12 and str(frame["hits.value"].dtype) == "Int64"
    assert bool(frame.iloc[1]["enabled"]) is False
    assert list(frame["layer"]) == ["Network"] * 3 and list(frame["layer.uid"]) == ["layer-uid"] * 3


def test_rulebase_pages_merge_dictionaries_and_count_rules(client, http):
    first = rulebase_reply()
    first.update({"to": 2, "total": 3})
    first["rulebase"] = first["rulebase"][:1]
    second = rulebase_reply()
    second.update({"from": 3, "to": 3, "total": 3})
    second["rulebase"] = second["rulebase"][1:]
    second["objects-dictionary"] = [{"uid": "act-drop", "name": "Drop", "type": "RulebaseAction"}]
    http[0].extend([(200, first), (200, second)])
    frame = client.execute_query({"table": "access_rules", "parent": {"layer": "Network"}, "fields": ["rule-number", "action", "track"]})
    assert [body_of(r)["offset"] for r in http[1][1:3]] == [0, 2]
    assert list(frame["rule-number"]) == [1, 2, 3]
    assert list(frame["track"]) == ["Log", "None", "Log"]


def test_nat_rulebase_resolves_translation_columns(client, http):
    http[0].append(
        (
            200,
            {
                "from": 1, "to": 2, "total": 2, "uid": "nat-layer",
                "rulebase": [
                    {"uid": "n1", "type": "nat-rule", "enabled": True, "rule-number": 1, "method": "static", "auto-generated": False,
                     "original-source": "any", "translated-source": "orig", "original-destination": "vip", "translated-destination": "web",
                     "original-service": "any", "translated-service": "orig", "install-on": ["pt"], "comments": ""},
                    {"uid": "s1", "name": "Manual Lower Rules", "type": "nat-section", "rulebase": [
                        {"uid": "n2", "type": "nat-rule", "enabled": True, "rule-number": 2, "method": "hide", "auto-generated": False,
                         "original-source": "net", "translated-source": "gw", "original-destination": "any", "translated-destination": "orig",
                         "original-service": "any", "translated-service": "orig", "install-on": ["pt"]}]},
                ],
                "objects-dictionary": [
                    {"uid": "any", "name": "Any", "type": "CpmiAnyObject"}, {"uid": "orig", "name": "Original", "type": "Global"},
                    {"uid": "vip", "name": "pub-vip", "type": "host"}, {"uid": "web", "name": "web-01", "type": "host"},
                    {"uid": "net", "name": "net-int", "type": "network"}, {"uid": "gw", "name": "gw1", "type": "simple-gateway"},
                    {"uid": "pt", "name": "Policy Targets", "type": "Global"},
                ],
            },
        )
    )
    frame = client.execute_query({"table": "nat_rules", "parent": {"package": "Standard"}})
    assert body_of(http[1][1])["package"] == "Standard" and body_of(http[1][1])["use-object-dictionary"] is True
    assert [None if pd.isna(x) else x for x in frame["section"]] == [None, "Manual Lower Rules"]
    assert list(frame["translated-destination"]) == ["web-01", "Original"]
    assert list(frame["translated-source.uid"]) == ["orig", "gw"]
    assert list(frame["method"]) == ["static", "hide"] and list(frame["package"]) == ["Standard", "Standard"]


def test_logs_query_uses_custom_window_type_and_query_id_paging(client, http):
    log = {"time": "2026-11-06T10:00:00Z", "action": "Drop", "src": "10.1.20.23", "dst": "10.1.30.31", "rule_name": "Cleanup rule",
           "blade": "Firewall", "bytes": "1200", "detected_on": ["a", "b"]}
    http[0].extend(
        [
            (200, {"logs": [log], "logs-count": "1", "query-id": "WEB_API_q1"}),
            (200, {"logs": [dict(log, src="10.1.20.24")], "logs-count": "1", "query-id": "WEB_API_q1"}),
            (200, {"logs": [], "logs-count": "0", "query-id": "WEB_API_q1"}),
        ]
    )
    frame = client.execute_query(
        {"table": "logs", "start_time": "2026-11-06T00:00:00Z", "end_time": "2026-11-07T00:00:00Z",
         "search": "action:Drop AND blade:Firewall", "fields": ["time", "src", "rule_name", "bytes", "detected_on"]}
    )
    first = body_of(http[1][1])["new-query"]
    assert first == {"type": "logs", "max-logs-per-request": 100, "filter": "action:Drop AND blade:Firewall", "time-frame": "custom",
                     "custom-start": "2026-11-06T00:00:00Z", "custom-end": "2026-11-07T00:00:00Z"}
    assert body_of(http[1][2]) == {"query-id": "WEB_API_q1"} and body_of(http[1][3]) == {"query-id": "WEB_API_q1"}
    assert list(frame["src"]) == ["10.1.20.23", "10.1.20.24"]
    assert frame.iloc[0]["detected_on"] == ["a", "b"]  # dynamic log fields stay structured
    assert commands(http[1])[-1] == "logout"


def test_logs_continuation_with_a_different_query_id_is_refused(client, http):
    http[0].extend(
        [
            (200, {"logs": [{"time": "t"}], "logs-count": "1", "query-id": "q1"}),
            (200, {"logs": [{"time": "t"}], "logs-count": "1", "query-id": "q2"}),
        ]
    )
    with pytest.raises(CheckPointError) as e:
        client.execute_query({"table": "logs", "lookback": "1h"})
    assert e.value.code == "InvalidResponse"


def test_audit_logs_use_type_audit_named_time_frame_and_stop_on_done(client, http):
    http[0].append((200, {"logs": [{"time": "2026-11-07T01:00:00Z", "operation": "Install Policy", "administrator": "admin"}],
                          "logs-count": "1", "query-id": "q", "query-status": "done"}))
    frame = client.execute_query({"table": "audit_logs", "time_frame": "last-7-days", "fields": ["operation", "administrator"]})
    query = body_of(http[1][1])["new-query"]
    assert query["type"] == "audit" and query["time-frame"] == "last-7-days" and "custom-start" not in query
    assert frame.iloc[0]["operation"] == "Install Policy"
    assert commands(http[1]) == ["login", "show-logs", "logout"]


def test_gateways_request_full_details_and_flatten_policy_and_blades(client, http):
    gw = obj("gw1", "simple-gateway", **{"ipv4-address": "10.0.0.1", "version": "R81.10", "operating-system": "Gaia",
                                        "sic-status": "communicating",
                                        "policy": {"access-policy-installed": True, "access-policy-name": "Standard",
                                                   "access-policy-installation-date": {"posix": 1700000000000, "iso-8601": "2023-11-14T22:13+0000"}},
                                        "network-security-blades": {"firewall": True, "ips": True}, "management-blades": {}})
    http[0].append((200, page([gw])))
    frame = client.execute_query(
        {"table": "gateways_and_servers",
         "fields": ["name", "version", "policy.access-policy-name", "policy.access-policy-installation-date.posix",
                    "network-security-blades.ips", "network-security-blades.anti-bot"]}
    )
    assert body_of(http[1][1])["details-level"] == "full"
    row = frame.iloc[0]
    assert row["policy.access-policy-name"] == "Standard" and row["policy.access-policy-installation-date.posix"] == 1700000000000
    assert bool(row["network-security-blades.ips"]) is True and row["network-security-blades.anti-bot"] is pd.NA
    assert str(frame["network-security-blades.anti-bot"].dtype) == "boolean"
    assert str(frame["policy.access-policy-installation-date.posix"].dtype) == "Int64"


def test_sessions_published_flag_and_integer_dtypes(client, http):
    http[0].append((200, page([obj("s1", "session", **{"user-name": "aa", "state": "open", "locks": 2, "changes": 0, "in-work": False})])))
    frame = client.execute_query({"table": "sessions", "view_published_sessions": True, "fields": ["user-name", "locks", "in-work"]})
    assert body_of(http[1][1])["view-published-sessions"] is True
    assert frame.iloc[0]["locks"] == 2 and str(frame["locks"].dtype) == "Int64"
    assert bool(frame.iloc[0]["in-work"]) is False


def test_tasks_send_status_initiator_and_window(client, http):
    http[0].append((200, {"from": 1, "to": 1, "total": 1, "tasks": [
        {"uid": "t", "task-id": "t", "task-name": "Install policy", "status": "failed", "progress-percentage": 100,
         "last-update-time": {"posix": 1, "iso-8601": "2026-01-01T00:00+0000"}, "task-details": [{"errors": [{"message": "SIC"}]}]}]}))
    frame = client.execute_query({"table": "tasks", "status": "failed", "initiator": "admin", "lookback": "7d"})
    body = body_of(http[1][1])
    assert body["status"] == "failed" and body["initiator"] == "admin" and body["details-level"] == "full"
    assert body["from-date"].endswith("Z") and body["to-date"] > body["from-date"]
    assert frame.iloc[0]["status"] == "failed" and frame.iloc[0]["task-details"][0]["errors"][0]["message"] == "SIC"


def test_changes_polls_show_task_until_the_diff_task_succeeds(client, http, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    session = {"session-uid": "s-1", "user-name": "aa", "publish-time": {"posix": 1485930056333, "iso-8601": "2017-02-01T08:20+0200"},
               "domain-info": {"uid": "d", "name": "SMC User", "domain-type": "domain"}}
    task = {"task-name": "Show Changes", "task-id": "task-1", "status": "succeeded", "progress-percentage": 100, "task-details": [
        {"changes": [{"session": session, "operations": {
            "added-objects": [{"uid": "a", "name": "MyRule3", "type": "access-rule"}],
            "modified-objects": [{"old-object": {"uid": "m", "name": "MyRule2", "type": "access-rule"}, "new-object": {"uid": "m", "name": "MyRule4", "type": "access-rule"}}],
            "deleted-objects": [{"uid": "d", "name": "MyRule1", "type": "access-rule"}]}}], "limit": 10, "offset": 0}]}
    http[0].extend(
        [
            (200, {"task-id": "task-1"}),
            (200, {"tasks": [{"task-id": "task-1", "status": "in progress", "progress-percentage": 40}]}),
            (200, {"tasks": [task]}),
        ]
    )
    frame = client.execute_query({"table": "changes", "lookback": "30d"})
    assert commands(http[1]) == ["login", "show-changes", "show-task", "show-task", "logout"]
    assert body_of(http[1][2]) == {"task-id": "task-1", "details-level": "full"}
    assert sorted(frame["operation"]) == ["added", "deleted", "modified"]
    modified = frame[frame["operation"] == "modified"].iloc[0]
    assert modified["object.name"] == "MyRule4" and modified["old-object.name"] == "MyRule2"
    assert frame.iloc[0]["session.user-name"] == "aa" and str(frame["session.publish-time.posix"].dtype) == "Int64"


def test_group_members_flatten_dereferenced_members(client, http):
    group = obj("grp", "group", members=[{"uid": "h1", "name": "web-01", "type": "host", "ipv4-address": "10.0.0.1"},
                                          {"uid": "n1", "name": "net", "type": "network", "subnet4": "10.1.0.0", "mask-length4": 16}])
    http[0].append((200, page([group])))
    frame = client.execute_query({"table": "group_members"})
    assert body_of(http[1][1])["dereference-group-members"] is True
    assert len(frame) == 2 and set(frame["group.name"]) == {"grp"}
    assert list(frame["member.type"]) == ["host", "network"] and frame.iloc[1]["member.mask-length4"] == 16


def test_client_side_filters_apply_after_fetch(client, http):
    http[0].append((200, page([obj("web-01", **{"ipv4-address": "10.1.10.11"}), obj("db-01", **{"ipv4-address": "10.1.30.31"}),
                               obj("app-01", **{"ipv4-address": "10.1.20.21"})])))
    frame = client.execute_query({"table": "hosts", "fields": ["name"], "filter": {"ipv4-address": {"op": "like", "value": "10.1.%.%1"},
                                                                                     "name": {"op": "in", "value": ["web-01", "db-01"]}}})
    assert sorted(frame["name"]) == ["db-01", "web-01"]
    assert "filter" not in body_of(http[1][1])  # client-side filter never becomes a server search


def test_server_search_and_order_are_passed_through(client, http):
    http[0].append((200, page([])))
    client.execute_query({"table": "hosts", "search": "10.1.20", "order_by": ["name desc"]})
    body = body_of(http[1][1])
    assert body["filter"] == "10.1.20" and body["order"] == [{"DESC": "name"}]


def test_api_versions_rows_and_test_connection_message(client, http):
    http[0].extend([(200, {"current-version": "1.9.1", "supported-versions": ["1.8", "1.9", "1.9.1"]})])
    frame = client.execute_query({"table": "api_versions"})
    assert list(frame["version"]) == ["1.8", "1.9", "1.9.1"] and list(frame["current"]) == [False, False, True]
    http[0].extend([(200, {"current-version": "1.9.1", "supported-versions": ["1.9", "1.9.1"]}),
                    (200, {"uid": "s", "user-name": "reader", "read-only": True, "connection-mode": "read only"})])
    result = client.test_connection()
    assert result["success"] and "1.9.1" in result["message"] and "reader" in result["message"]
    assert commands(http[1])[-4:] == ["login", "show-api-versions", "show-session", "logout"]


def test_test_connection_rejects_unsupported_pinned_version(http):
    c = CheckPointClient(url="https://mgmt.example", username="r", password="p", api_version="1.7")
    http[0].append((200, {"current-version": "1.9.1", "supported-versions": ["1.9", "1.9.1"]}))
    with pytest.raises(CheckPointError) as e:
        c.test_connection()
    assert e.value.code == "UnsupportedVersion"
    c.close()


def test_transient_errors_retry_then_return_complete_data(client, http, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    http[0].extend([(503, {}), (200, page([obj("a")]))])
    frame = client.execute_query({"table": "hosts", "fields": ["name"]})
    assert len(frame) == 1 and commands(http[1]) == ["login", "show-hosts", "show-hosts", "logout"]


def test_error_envelope_on_http_200_is_not_data(client, http):
    http[0].append((200, {"code": "generic_err_object_not_found", "message": "Requested object [x] not found"}))
    with pytest.raises(CheckPointError) as e:
        client.execute_query({"table": "hosts"})
    assert e.value.code == "RemoteError" and e.value.vendor_code == "generic_err_object_not_found"


def test_retrieval_timestamp_and_provenance_columns(client, http):
    http[0].append((200, page([obj("a")])))
    client._bow_connection_id = "conn-1"
    frame = client.execute_query({"table": "hosts", "fields": ["name"]})
    assert str(frame["_bow_retrieved_at"].dtype) == "datetime64[ns, UTC]"
    assert frame.iloc[0]["_bow_connection"] == "conn-1" and frame.iloc[0]["_bow_management"] == "mgmt.example"
    assert frame.iloc[0]["_bow_command"] == "show-hosts"


def test_query_alias_and_async_wrapper_use_same_contract(client, http):
    import asyncio

    http[0].extend([(200, page([])), (200, page([]))])
    assert client.query({"table": "hosts"}).empty
    assert asyncio.run(client.aexecute_query({"table": "hosts"})).empty


def test_rca_query_runs_through_bow_code_executor(client, http, monkeypatch):
    from app.ai.code_execution.code_execution import StreamingCodeExecutor

    # The executor's subprocess sandbox needs an importable venv; the in-process mode exercises the same contract.
    monkeypatch.setenv("BOW_CODE_SANDBOX", "inprocess")

    http[0].append((200, rulebase_reply()))
    code = """
def generate_df(ds_clients, excel_files):
    df = ds_clients["cp"].execute_query({"table":"access_rules","parent":{"layer":"Network"},"fields":["rule-number","name","enabled","action","source","destination","service"]})
    any_any = df[(df["source"].apply(lambda s: s == ["Any"])) & (df["destination"].apply(lambda s: s == ["Any"])) & (df["service"].apply(lambda s: s == ["Any"])) & df["enabled"].fillna(False)]
    return any_any[["rule-number", "name", "action"]]
"""
    client._bow_connection_id = "synthetic-connection"
    frame, _, queries = StreamingCodeExecutor().execute_code(code=code, ds_clients={"cp": client}, excel_files=[])
    assert list(frame["name"]) == ["Cleanup rule"]
    assert len(queries) == 1 and json.loads(queries[0])["table"] == "access_rules"


# ----------------------------------------------------------------- in-process simulator
@pytest.fixture(scope="module")
def simulator():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[3] / "tools/checkpoint/simulated_api.py"
    spec = importlib.util.spec_from_file_location("checkpoint_simulated_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    httpd, api = module.make_server(0, {"CHECKPOINT_USERNAME": "reader", "CHECKPOINT_PASSWORD": "synthetic-only", "CHECKPOINT_API_KEY": "synthetic-key"})
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", module
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_simulator_end_to_end_over_http(simulator):
    url, _ = simulator
    client = CheckPointClient(url=url, username="reader", password="synthetic-only", allow_http=True)
    try:
        result = client.test_connection()
        assert result["success"] and "read-only" in result["message"]
        for table in PRIMARY:
            query = {"table": table}
            if table in {"access_rules"}:
                query["parent"] = {"layer": "Standard Network"}
            elif table == "nat_rules":
                query["parent"] = {"package": "Standard"}
            elif table in {"logs", "audit_logs"}:
                query["lookback"] = "48h"
            elif table == "changes":
                query["lookback"] = "30d"
            frame = client.execute_query(query)
            assert len(frame) > 0, table
            assert str(frame["_bow_retrieved_at"].dtype) == "datetime64[ns, UTC]"
        rules = client.execute_query({"table": "access_rules", "parent": {"layer": "Standard Network"}, "filter": {"enabled": False}})
        assert len(rules) == 3 and all(not e for e in rules["enabled"])
        drops = client.execute_query({"table": "logs", "lookback": "48h", "search": "action:Drop AND src:10.1.20.23", "fields": ["src", "dst", "rule_name"]})
        assert len(drops) > 0 and set(drops["rule_name"]) == {"Cleanup rule"} and set(drops["src"]) == {"10.1.20.23"}
        stale = client.execute_query({"table": "sessions", "filter": {"in-work": False}})
        assert list(stale["user-name"]) == ["svc-automation"] and int(stale.iloc[0]["locks"]) == 1
        with pytest.raises(CheckPointError) as e:
            client.execute_query({"table": "access_rules", "parent": {"layer": "No Such Layer"}})
        assert e.value.code == "UnsupportedResource" and e.value.vendor_code == "generic_err_object_not_found"
    finally:
        client.close()
    with_key = CheckPointClient(url=url, api_key="synthetic-key", allow_http=True)
    try:
        assert with_key.test_connection()["success"]
    finally:
        with_key.close()


def test_simulator_enforces_the_documented_auth_and_read_only_contract(simulator):
    url, _ = simulator
    s = requests.Session()
    s.trust_env = False
    bad = s.post(url + "/web_api/login", json={"user": "reader", "password": "wrong"})
    assert bad.status_code == 400 and bad.json() == {"code": "err_login_failed", "message": "Authentication to server failed."}
    missing = s.post(url + "/web_api/show-hosts", json={})
    assert missing.status_code == 401 and missing.json()["code"] == "generic_err_missing_session_id"
    wrong = s.post(url + "/web_api/show-hosts", json={}, headers={"X-chkp-sid": "nope"})
    assert wrong.status_code == 401 and wrong.json()["code"] == "generic_err_wrong_session_id"
    login = s.post(url + "/web_api/v1.9.1/login", json={"user": "reader", "password": "synthetic-only", "read-only": True}).json()
    assert login["read-only"] is True and login["api-server-version"] == "1.9.1" and login["session-timeout"] == 600
    sid = {"X-chkp-sid": login["sid"]}
    for command in ("publish", "add-host", "set-access-rule", "install-policy", "run-script", "discard", "delete-host"):
        r = s.post(f"{url}/web_api/{command}", json={"name": "x"}, headers=sid)
        assert r.status_code == 403 and r.json()["code"] == "generic_err_no_permissions", command
    unknown = s.post(url + "/web_api/show-unicorns", json={}, headers=sid)
    assert unknown.status_code == 404 and unknown.json()["code"] == "generic_err_command_not_found"
    too_big = s.post(url + "/web_api/show-hosts", json={"limit": 501}, headers=sid)
    assert too_big.status_code == 400 and too_big.json()["code"] == "generic_err_invalid_parameter"
    typo = s.post(url + "/web_api/show-hosts", json={"limitt": 5}, headers=sid)
    assert typo.status_code == 400 and typo.json()["code"] == "generic_err_invalid_parameter_name"
    hosts = s.post(url + "/web_api/show-hosts", json={"limit": 5, "offset": 5, "details-level": "uid"}, headers=sid).json()
    assert hosts["from"] == 6 and hosts["to"] == 10 and hosts["total"] >= 15 and all(isinstance(u, str) for u in hosts["objects"])
    assert s.post(url + "/web_api/logout", json={}, headers=sid).json() == {"message": "OK"}
    after = s.post(url + "/web_api/show-hosts", json={}, headers=sid)
    assert after.status_code == 401 and after.json()["code"] == "generic_err_wrong_session_id"





# ----------------------------------------------------------------------------- LLM-usability regressions (live Haiku run)
def test_provenance_columns_may_be_requested_in_fields(client, http):
    queue, seen = http
    queue.append((200, {"objects": [{"uid": "u1", "name": "h1", "type": "host", "ipv4-address": "10.0.0.1"}], "from": 1, "to": 1, "total": 1}))
    df = client.execute_query({"table": "hosts", "fields": ["name", "_bow_retrieved_at", "_bow_management"]})
    assert list(df["name"]) == ["h1"] and "_bow_retrieved_at" in df.columns
    with pytest.raises(CheckPointError) as e:
        client.execute_query({"table": "hosts", "fields": ["name", "_bow_secret"]})
    assert e.value.code == "InvalidQuery"


def test_limit_error_explains_ceiling_semantics(client, http):
    queue, seen = http
    queue.append((200, {"objects": [{"uid": f"u{i}", "name": f"h{i}", "type": "host"} for i in range(3)], "from": 1, "to": 3, "total": 3}))
    with pytest.raises(CheckPointError) as e:
        client.execute_query({"table": "hosts", "limit": 2})
    assert e.value.code == "ResultLimitExceeded"
    assert "head(" in str(e.value) and "ceiling" in str(e.value)


def test_open_ended_and_future_windows_are_clamped_to_now(client, http):
    """Planners write start_time=yesterday with no end, or end_time=tomorrow; both mean "up to now"."""
    queue, seen = http
    for spec in (
        {"table": "audit_logs", "start_time": "2026-11-06T00:00:00Z"},
        {"table": "audit_logs", "start_time": "2026-11-06T00:00:00Z", "end_time": "2999-01-01T00:00:00Z"},
    ):
        queue.append((200, {"query-id": "q1", "logs": [{"time": "2026-11-06T01:00:00Z", "operation": "x"}], "logs-count": "1"}))
        queue.append((200, {"query-id": "q1", "logs": [], "logs-count": "0"}))
        before = len(seen)
        df = client.execute_query(spec)
        assert len(df) == 1
        first = next(r for r in seen[before:] if urlsplit(r.url).path.endswith("show-logs"))
        end = body_of(first)["new-query"]["custom-end"]
        assert not end.startswith("2999")
