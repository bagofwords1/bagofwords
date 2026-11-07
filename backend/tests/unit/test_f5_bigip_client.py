"""F5 BIG-IP iControl REST contracts exercised at the HTTP boundary; no appliance credentials."""

import io
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from app.data_sources.clients.f5_bigip_client import F5BigIpClient, F5BigIpError, _catalog
from app.schemas.data_source_registry import resolve_client_class

LOCAL = "https://localhost"
PRIMARY = [
    "virtual_servers",
    "virtual_server_stats",
    "pools",
    "pool_stats",
    "pool_members",
    "pool_member_stats",
    "nodes",
    "node_stats",
    "irules",
    "profiles_http",
    "profiles_client_ssl",
    "ssl_certs",
    "sys_version",
    "sys_hardware",
    "cm_devices",
    "cm_failover_status",
    "sys_cpu",
    "sys_memory",
    "sys_clock",
    "ltm_monitors_http",
    "net_interfaces",
    "net_vlans",
    "net_self",
]


def test_registry_resolves_client_and_table_contract():
    cls = resolve_client_class("f5_bigip")
    assert cls is F5BigIpClient
    client = cls(url="https://bigip.example", username="reader", password="test-only")
    tables = {t.name: t for t in client.get_tables()}
    assert set(PRIMARY) <= tables.keys()
    assert tables["pool_members"].metadata_json["f5_bigip"]["parents"] == ["pool"]
    assert tables["pool_member_stats"].metadata_json["f5_bigip"]["endpoint"] == "/mgmt/tm/ltm/pool/{pool}/members/stats"
    assert any(c.name == "status.availabilityState" for c in tables["pool_member_stats"].columns)
    assert any(fk.references_name == "ssl_certs" for fk in tables["profiles_client_ssl"].fks)
    assert len(client.prompt_schema().splitlines()) == len(tables) and len(client.prompt_schema()) < 6000


@pytest.fixture
def client():
    c = F5BigIpClient(url="https://bigip.example", username="reader", password="test-only")
    yield c
    c.close()


def _response(request, status, body):
    r = requests.Response()
    r.status_code = status
    r.raw = io.BytesIO(json.dumps(body).encode())
    r.url = request.url
    r.request = request
    return r


@pytest.fixture
def http(monkeypatch):
    """Answer the token handshake itself; GETs come from `queue`; every request is recorded."""
    state = {"queue": [], "requests": [], "tokens": [], "deleted": [], "login_status": 200}

    def send(session, request, **kwargs):
        state["requests"].append(request)
        path = urlsplit(request.url).path
        if request.method == "POST" and path == "/mgmt/shared/authn/login":
            if state["login_status"] != 200:
                return _response(request, state["login_status"], {"code": 401, "message": "Authentication failed."})
            token = f"TOKEN{len(state['tokens']) + 1:02d}" + "Q" * 20
            state["tokens"].append(token)
            return _response(request, 200, {"username": "reader", "token": {"token": token, "timeout": 1200}})
        if request.method == "DELETE":
            state["deleted"].append(path.rsplit("/", 1)[-1])
            return _response(request, 200, {})
        assert request.method == "GET", request.method
        status, body = state["queue"].pop(0)
        return _response(request, status, body)

    monkeypatch.setattr(requests.Session, "send", send)
    state["gets"] = lambda: [r for r in state["requests"] if r.method == "GET"]
    return state


def _collection(items, **extra):
    return {"kind": "tm:ltm:pool:poolcollectionstate", "selfLink": f"{LOCAL}/mgmt/tm/ltm/pool?ver=17.1.1", "items": items, **extra}


def _stats(path, objects, entries_for):
    return {
        "kind": "tm:ltm:pool:poolcollectionstats",
        "selfLink": f"{LOCAL}{path}/stats?ver=17.1.1",
        "entries": {f"{LOCAL}{path}/{o.replace('/', '~')}/stats": {"nestedStats": {"entries": entries_for(o)}} for o in objects},
    }


def _version(version="17.1.1"):
    return {
        "kind": "tm:sys:version:versionstats",
        "selfLink": f"{LOCAL}/mgmt/tm/sys/version?ver={version}",
        "entries": {
            f"{LOCAL}/mgmt/tm/sys/version/0": {
                "nestedStats": {
                    "entries": {
                        "Build": {"description": "0.0.2"},
                        "Edition": {"description": "Point Release 1"},
                        "Product": {"description": "BIG-IP"},
                        "Version": {"description": version},
                    }
                }
            }
        },
    }


# ----------------------------------------------------------------------------- auth handshake
def test_token_login_header_and_logout_follow_icontrol_contract(client, http):
    http["queue"].append((200, _collection([{"name": "p", "partition": "Common", "fullPath": "/Common/p"}])))
    client.execute_query({"table": "pools"})
    login = http["requests"][0]
    assert login.method == "POST" and urlsplit(login.url).path == "/mgmt/shared/authn/login"
    assert login.headers["Content-Type"] == "application/json"
    assert json.loads(login.body) == {"username": "reader", "password": "test-only", "loginProviderName": "tmos"}
    get = http["gets"]()[0]
    assert get.headers["X-F5-Auth-Token"] == http["tokens"][0]
    assert "Authorization" not in get.headers and "Authorization" not in login.headers
    logout = http["requests"][-1]
    assert logout.method == "DELETE"
    assert urlsplit(logout.url).path == "/mgmt/shared/authz/tokens/" + http["tokens"][0]
    assert logout.headers["X-F5-Auth-Token"] == http["tokens"][0]
    assert http["deleted"] == http["tokens"]


def test_custom_login_provider_is_sent(http):
    c = F5BigIpClient(url="https://bigip.example", username="ad-reader", password="x", login_provider="corp-ldap")
    http["queue"].append((200, _version()))
    c.execute_query({"table": "sys_version"})
    assert json.loads(http["requests"][0].body)["loginProviderName"] == "corp-ldap"


def test_failed_login_is_authentication_failed_without_data_requests(client, http):
    http["login_status"] = 401
    with pytest.raises(F5BigIpError) as e:
        client.execute_query({"table": "pools"})
    assert e.value.code == "AuthenticationFailed"
    assert not http["gets"]() and not http["deleted"]


def test_expired_token_triggers_one_relogin_then_retry(client, http):
    http["queue"].extend(
        [
            (401, {"code": 401, "message": "X-F5-Auth-Token has expired."}),
            (200, _collection([{"name": "p", "partition": "Common", "fullPath": "/Common/p"}])),
        ]
    )
    df = client.execute_query({"table": "pools"})
    assert len(df) == 1
    assert len(http["tokens"]) == 2
    gets = http["gets"]()
    assert gets[0].headers["X-F5-Auth-Token"] == http["tokens"][0]
    assert gets[1].headers["X-F5-Auth-Token"] == http["tokens"][1]
    assert http["deleted"] == [http["tokens"][1]]


def test_second_unauthorized_after_relogin_is_terminal(client, http):
    http["queue"].extend([(401, {"code": 401, "message": "x"}), (401, {"code": 401, "message": "x"})])
    with pytest.raises(F5BigIpError) as e:
        client.execute_query({"table": "pools"})
    assert e.value.code == "AuthenticationFailed" and e.value.f5_code == 401
    assert len(http["gets"]()) == 2 and len(http["tokens"]) == 2


# ----------------------------------------------------------------------------- paging
def test_top_skip_paging_preserves_scope_and_stops_on_short_or_empty_page(client, http):
    nxt = f"{LOCAL}/mgmt/tm/ltm/pool?$filter=partition+eq+App1&$select=name%2Cpartition%2CfullPath%2Cmonitor&$top=500&$skip=500&ver=17.1.1"
    http["queue"].extend(
        [
            (200, _collection([{"fullPath": "/App1/a", "monitor": "/Common/http"}], nextLink=nxt, totalItems=3)),
            (200, _collection([{"fullPath": "/App1/b"}], nextLink=nxt.replace("$skip=500", "$skip=1000"), totalItems=3)),
            (200, _collection([], nextLink=nxt.replace("$skip=500", "$skip=1500"))),
        ]
    )
    df = client.execute_query({"table": "pools", "filter": {"partition": "App1"}, "fields": ["fullPath", "monitor"]})
    assert list(df["fullPath"]) == ["/App1/a", "/App1/b"]
    assert df["monitor"].isna().tolist() == [False, True]
    gets = http["gets"]()
    assert len(gets) == 3
    for i, r in enumerate(gets):
        params = parse_qs(urlsplit(r.url).query)
        assert params["$filter"] == ["partition eq App1"]
        assert params["$select"] == ["name,partition,fullPath,monitor"]
        assert params["$top"] == ["500"]
        assert params.get("$skip", ["0"]) == [str(500 * i)]
    assert urlsplit(gets[1].url).netloc == "bigip.example"  # the localhost nextLink host is never followed


@pytest.mark.parametrize(
    "href",
    [
        f"{LOCAL}/mgmt/tm/ltm/node?$top=500&$skip=500&ver=17.1.1",
        f"{LOCAL}/mgmt/tm/ltm/pool?$top=500&$skip=500&expandSubcollections=true&ver=17.1.1",
        f"{LOCAL}/mgmt/tm/ltm/pool?$filter=partition+eq+Other&$top=500&$skip=500&ver=17.1.1",
        f"{LOCAL}/mgmt/tm/ltm/pool?$top=500&$skip=0&ver=17.1.1",
        f"{LOCAL}/mgmt/tm/ltm/pool?$top=500&$skip=abc&ver=17.1.1",
    ],
)
def test_continuation_cannot_change_resource_scope_or_repeat(client, http, href):
    http["queue"].append((200, _collection([{"fullPath": "/Common/a"}], nextLink=href)))
    with pytest.raises(F5BigIpError) as e:
        client.execute_query({"table": "pools", "filter": {"partition": "Common"}} if "Other" in href else {"table": "pools"})
    assert e.value.code == "InvalidResponse"
    assert len(http["gets"]()) == 1


def test_complete_result_limit_never_silently_truncates(client, http):
    nxt = f"{LOCAL}/mgmt/tm/ltm/pool?$top=2&$skip=2&ver=17.1.1"
    http["queue"].extend([(200, _collection([{"fullPath": "/Common/a"}, {"fullPath": "/Common/b"}], nextLink=nxt)), (200, _collection([{"fullPath": "/Common/c"}]))])
    with pytest.raises(F5BigIpError) as e:
        client.execute_query({"table": "pools", "limit": 2})
    assert e.value.code == "ResultLimitExceeded"
    assert parse_qs(urlsplit(http["gets"]()[0].url).query)["$top"] == ["2"]


def test_stats_limit_is_enforced_on_the_complete_snapshot(client, http):
    http["queue"].append((200, _stats("/mgmt/tm/ltm/node", ["/Common/a", "/Common/b"], lambda o: {"addr": {"description": "10.0.0.1"}})))
    with pytest.raises(F5BigIpError) as e:
        client.execute_query({"table": "node_stats", "limit": 1})
    assert e.value.code == "ResultLimitExceeded"


# ----------------------------------------------------------------------------- errors
@pytest.mark.parametrize(
    "status,code", [(401, "AuthenticationFailed"), (403, "PermissionDenied"), (404, "UnsupportedResource"), (400, "InvalidRequest")]
)
def test_http_errors_map_to_codes_without_echoing_bodies(client, http, status, code):
    http["queue"].append((status, {"code": status, "message": "01020036:3: The requested Pool (/Common/customer-secret) was not found.", "errorStack": []}))
    if status == 401:
        http["queue"].append((status, {"code": status, "message": "customer-secret"}))
    with pytest.raises(F5BigIpError) as e:
        client.execute_query({"table": "pools", "name": "/Common/customer-secret"})
    assert e.value.code == code and e.value.status == status and e.value.f5_code == status
    assert "customer-secret" not in str(e.value) and "01020036" not in str(e.value)
    assert http["deleted"], "logout must still happen after an error"


def test_transient_errors_retry_then_return_complete_data(client, http, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    http["queue"].extend([(503, {}), (200, _collection([{"fullPath": "/Common/a"}]))])
    assert len(client.execute_query({"table": "pools"})) == 1
    assert len(http["gets"]()) == 2


# ----------------------------------------------------------------------------- validation (no HTTP)
@pytest.mark.parametrize(
    "query",
    [
        {"table": "nope"},
        {"table": "pools", "url": "/mgmt/tm/sys"},
        {"table": "pools", "fields": ["password"]},
        {"table": "pools", "fields": []},
        {"table": "pools", "filter": {"bogus": 1}},
        {"table": "pools", "filter": {"partition": "Common eq x"}},
        {"table": "pools", "filter": {"monitor": {"op": "eq"}}},
        {"table": "pools", "limit": True},
        {"table": "pools", "limit": 0},
        {"table": "pools", "name": "web_pool"},
        {"table": "pools", "name": "/Common/../sys"},
        {"table": "pools", "name": "/Common/a b"},
        {"table": "pools", "parent": {"pool": "/Common/x"}},
        {"table": "pool_members", "parent": {"pool": "../../shared/authz/tokens"}},
        {"table": "pool_members", "parent": {"node": "/Common/x"}},
        {"table": "pool_members", "name": "/Common/web01:80"},
        {"table": "sys_version", "name": "/Common/x"},
        {"table": "pool_member_stats", "parent": {"pool": "Common/web_pool"}},
        "not json {",
        42,
    ],
)
def test_invalid_or_unsafe_queries_make_no_http_request(client, http, query):
    with pytest.raises(F5BigIpError) as e:
        client.execute_query(query)
    assert e.value.code == "InvalidQuery"
    assert not http["requests"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"url": "http://bigip.example", "allow_http": True},
        {"url": "http://127.0.0.1:8443"},
        {"url": "https://bigip.example/mgmt/tm"},
        {"url": "https://user:pw@bigip.example"},
        {"url": "https://bigip.example", "username": ""},
        {"url": "https://bigip.example", "login_provider": "a b"},
        {"url": "https://bigip.example", "timeout": 0},
        {"url": "https://bigip.example", "max_rows": 10**9},
    ],
)
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(F5BigIpError) as e:
        F5BigIpClient(**{"username": "reader", "password": "x", **kwargs})
    assert e.value.code in {"InvalidConfiguration", "InvalidQuery"}


def test_request_allow_list_blocks_every_mutation_and_uncatalogued_read(client, http):
    with requests.Session() as session:
        for method, path in [
            ("POST", "/mgmt/tm/ltm/pool"),
            ("PUT", "/mgmt/tm/ltm/pool/~Common~a"),
            ("PATCH", "/mgmt/shared/authz/tokens/TOKEN"),
            ("DELETE", "/mgmt/tm/ltm/pool/~Common~a"),
            ("POST", "/mgmt/tm/util/bash"),
            ("GET", "/mgmt/tm/util/bash"),
            ("GET", "/mgmt/tm/sys/file/ssl-key"),
            ("GET", "/mgmt/shared/authz/tokens"),
            ("GET", "/mgmt/tm/ltm/pool/members"),
            ("GET", "/mgmt/tm/ltm/virtual/~Common~a/members"),
        ]:
            with pytest.raises(F5BigIpError) as e:
                client._send(session, method, path, None, None, float("inf"))
            assert e.value.code == "ForbiddenOperation", (method, path)
    assert not http["requests"]


def test_malformed_catalog_is_rejected_before_any_http(client, http, monkeypatch):
    original = Path.read_text
    catalog = json.loads(original(Path(__file__).parents[2] / "app/data_sources/clients/f5_bigip/catalog.json"))
    catalog["tables"]["pool_members"]["parents"] = []

    def read(path, *args, **kwargs):
        return json.dumps(catalog) if path.name == "catalog.json" and path.parent.name == "f5_bigip" else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    _catalog.cache_clear()
    try:
        with pytest.raises(F5BigIpError) as e:
            client.get_tables()
        assert e.value.code == "InvalidCatalog" and not http["requests"]
    finally:
        _catalog.cache_clear()


# ----------------------------------------------------------------------------- request shapes per table
@pytest.mark.parametrize(
    "query,path,params",
    [
        ({"table": "virtual_servers"}, "/mgmt/tm/ltm/virtual", {"$top"}),
        ({"table": "virtual_server_stats"}, "/mgmt/tm/ltm/virtual/stats", set()),
        ({"table": "pools", "filter": {"partition": "App1"}}, "/mgmt/tm/ltm/pool", {"$top", "$filter"}),
        ({"table": "pool_stats"}, "/mgmt/tm/ltm/pool/stats", set()),
        ({"table": "pool_members", "parent": {"pool": "/Common/web_pool"}}, "/mgmt/tm/ltm/pool/~Common~web_pool/members", {"$top"}),
        ({"table": "pool_member_stats", "parent": {"pool": "/App1/app1 pool".replace(" ", "_")}}, "/mgmt/tm/ltm/pool/~App1~app1_pool/members/stats", set()),
        ({"table": "nodes", "name": "/Common/web01"}, "/mgmt/tm/ltm/node/~Common~web01", set()),
        ({"table": "node_stats", "name": "/Common/web01"}, "/mgmt/tm/ltm/node/~Common~web01/stats", set()),
        ({"table": "pool_members", "parent": {"pool": "/Common/p"}, "name": "/Common/10.1.1.1%2:80"}, "/mgmt/tm/ltm/pool/~Common~p/members/~Common~10.1.1.1%252:80", set()),
        ({"table": "irules"}, "/mgmt/tm/ltm/rule", {"$top"}),
        ({"table": "profiles_client_ssl", "fields": ["fullPath", "cert"]}, "/mgmt/tm/ltm/profile/client-ssl", {"$top", "$select"}),
        ({"table": "ssl_certs"}, "/mgmt/tm/sys/file/ssl-cert", {"$top"}),
        ({"table": "ltm_monitors_http"}, "/mgmt/tm/ltm/monitor/http", {"$top"}),
        ({"table": "cm_devices"}, "/mgmt/tm/cm/device", {"$top"}),
        ({"table": "cm_failover_status"}, "/mgmt/tm/cm/failover-status", set()),
        ({"table": "sys_version"}, "/mgmt/tm/sys/version", set()),
        ({"table": "sys_hardware"}, "/mgmt/tm/sys/hardware", set()),
        ({"table": "sys_cpu"}, "/mgmt/tm/sys/cpu", set()),
        ({"table": "sys_memory"}, "/mgmt/tm/sys/memory", set()),
        ({"table": "net_interfaces"}, "/mgmt/tm/net/interface", {"$top"}),
        ({"table": "net_vlans"}, "/mgmt/tm/net/vlan", {"$top"}),
        ({"table": "net_self"}, "/mgmt/tm/net/self", {"$top"}),
    ],
)
def test_each_table_sends_exactly_its_documented_request(client, http, query, path, params):
    kind = _catalog()["tables"][query["table"]]["kind"]
    body = {} if "name" in query and kind == "collection" else _collection([]) if kind == "collection" else {"kind": "x", "entries": {}}
    http["queue"].append((200, body))
    frame = client.execute_query(query)
    request = http["gets"]()[0]
    assert request.url.split("?")[0] == "https://bigip.example" + path
    sent = parse_qs(urlsplit(request.url).query)
    assert set(sent) == params
    if "$select" in params:
        assert sent["$select"] == ["name,partition,fullPath,cert"]
    assert str(frame["_bow_retrieved_at"].dtype) == "datetime64[ns, UTC]"
    assert len(http["gets"]()) == 1


def test_select_projects_top_level_of_dotted_fields(client, http):
    http["queue"].append((200, _collection([{"fullPath": "/Common/vs", "sourceAddressTranslation": {"type": "automap"}, "vlans": ["/Common/a"]}])))
    df = client.execute_query({"table": "virtual_servers", "fields": ["fullPath", "sourceAddressTranslation.type", "vlans"]})
    assert parse_qs(urlsplit(http["gets"]()[0].url).query)["$select"] == ["name,partition,fullPath,sourceAddressTranslation,vlans"]
    assert df.iloc[0]["sourceAddressTranslation.type"] == "automap" and df.iloc[0]["vlans"] == ["/Common/a"]
    assert list(df.columns)[:3] == ["fullPath", "sourceAddressTranslation.type", "vlans"]


# ----------------------------------------------------------------------------- shaping
def test_stats_flatten_values_descriptions_and_object_identity(client, http):
    def entries(o):
        return {
            "status.availabilityState": {"description": "offline" if o.endswith("b") else "available"},
            "serverside.curConns": {"value": 9007199254740993},
            "totRequests": {"value": 5},
            "tmName": {"description": o},
            "someNewCounter": {"value": 1},
        }

    http["queue"].append((200, _stats("/mgmt/tm/ltm/pool", ["/Common/a", "/App1/b"], entries)))
    df = client.execute_query({"table": "pool_stats"})
    assert list(df["_object"]) == ["/Common/a", "/App1/b"]
    assert df.iloc[0]["serverside.curConns"] == 9007199254740993 and str(df["serverside.curConns"].dtype) == "Int64"
    assert str(df["status.availabilityState"].dtype) == "string"
    assert "someNewCounter" in df.columns and str(df["someNewCounter"].dtype) == "Int64"
    assert "status.enabledState" in df.columns and df["status.enabledState"].isna().all()


def test_stats_partition_and_field_filters_apply_client_side_only(client, http):
    http["queue"].append((200, _stats("/mgmt/tm/ltm/node", ["/Common/a", "/App1/b", "/App1/c"], lambda o: {"monitorStatus": {"description": "down" if o.endswith("c") else "up"}})))
    df = client.execute_query({"table": "node_stats", "filter": {"partition": "App1", "monitorStatus": ["down", "forced-down"]}})
    assert list(df["_object"]) == ["/App1/c"]
    assert not parse_qs(urlsplit(http["gets"]()[0].url).query)


def test_nested_singletons_collapse_to_one_row_with_lists_preserved(client, http):
    http["queue"].append(
        (
            200,
            {
                "kind": "tm:cm:failover-status:failover-statusstats",
                "entries": {
                    f"{LOCAL}/mgmt/tm/cm/failover-status/0": {
                        "nestedStats": {
                            "entries": {
                                "color": {"description": "green"},
                                f"{LOCAL}/mgmt/tm/cm/failoverStatus/0/details": {
                                    "nestedStats": {
                                        "entries": {
                                            f"{LOCAL}/mgmt/tm/cm/failoverStatus/0/details/0": {"nestedStats": {"entries": {"details": {"description": "active for /Common/traffic-group-1"}}}},
                                            f"{LOCAL}/mgmt/tm/cm/failoverStatus/0/details/1": {"nestedStats": {"entries": {"details": {"description": "standby for /Common/traffic-group-2"}}}},
                                        }
                                    }
                                },
                                "status": {"description": "ACTIVE"},
                                "summary": {"description": "1/2 active"},
                            }
                        }
                    }
                },
            },
        )
    )
    df = client.execute_query({"table": "cm_failover_status"})
    assert len(df) == 1
    assert df.iloc[0]["status"] == "ACTIVE" and df.iloc[0]["details"] == ["active for /Common/traffic-group-1", "standby for /Common/traffic-group-2"]


def test_sys_cpu_rows_inherit_host_scalars(client, http):
    cpu = f"{LOCAL}/mgmt/tm/sys/cpu"
    http["queue"].append(
        (
            200,
            {
                "kind": "tm:sys:cpu:cpustats",
                "entries": {
                    f"{cpu}/0": {
                        "nestedStats": {
                            "entries": {
                                f"{cpu}/0/cpuInfo": {
                                    "nestedStats": {
                                        "entries": {
                                            f"{cpu}/0/cpuInfo/{i}": {"nestedStats": {"entries": {"cpuId": {"value": i}, "fiveSecAvgIdle": {"value": 90 - i}}}} for i in range(3)
                                        }
                                    }
                                },
                                "hostId": {"description": "0"},
                            }
                        }
                    }
                },
            },
        )
    )
    df = client.execute_query({"table": "sys_cpu", "fields": ["hostId", "cpuId", "fiveSecAvgIdle"]})
    assert list(df["cpuId"]) == [0, 1, 2] and set(df["hostId"]) == {"0"} and list(df["fiveSecAvgIdle"]) == [90, 89, 88]


def test_single_object_name_returns_one_row_and_members_keep_port(client, http):
    http["queue"].append((200, {"kind": "tm:ltm:pool:members:membersstate", "name": "web01:80", "partition": "Common", "fullPath": "/Common/web01:80", "state": "down", "session": "monitor-enabled"}))
    df = client.execute_query({"table": "pool_members", "parent": {"pool": "/Common/web_pool"}, "name": "/Common/web01:80"})
    assert urlsplit(http["gets"]()[0].url).path == "/mgmt/tm/ltm/pool/~Common~web_pool/members/~Common~web01:80"
    assert len(df) == 1 and df.iloc[0]["pool"] == "/Common/web_pool" and df.iloc[0]["state"] == "down"


def test_member_crawl_expands_subcollections_and_filters_partition_client_side(client, http):
    pools = [
        {"fullPath": "/Common/p1", "membersReference": {"link": "x", "isSubcollection": True, "items": [{"fullPath": "/Common/n1:80", "partition": "Common", "state": "up"}]}},
        {"fullPath": "/App1/p2", "membersReference": {"link": "x", "isSubcollection": True, "items": [{"fullPath": "/App1/n2:80", "partition": "App1", "state": "down"}]}},
    ]
    http["queue"].append((200, _collection(pools)))
    df = client.execute_query({"table": "pool_members", "filter": {"partition": "App1"}, "fields": ["fullPath", "state", "pool"]})
    sent = parse_qs(urlsplit(http["gets"]()[0].url).query)
    assert sent["expandSubcollections"] == ["true"] and "$filter" not in sent and "$select" not in sent
    assert urlsplit(http["gets"]()[0].url).path == "/mgmt/tm/ltm/pool"
    assert len(df) == 1
    assert df.iloc[0][["fullPath", "state", "pool"]].to_dict() == {"fullPath": "/App1/n2:80", "state": "down", "pool": "/App1/p2"}


def test_member_stats_crawl_makes_one_bounded_request_per_pool(client, http):
    http["queue"].append((200, _collection([{"fullPath": "/Common/p1"}, {"fullPath": "/App1/p2"}])))
    for pool in ["/Common/p1", "/App1/p2"]:
        base = f"/mgmt/tm/ltm/pool/{pool.replace('/', '~')}/members"
        http["queue"].append((200, {"kind": "tm:ltm:pool:members:memberscollectionstats", "entries": {f"{LOCAL}{base}/~Common~n:80/stats": {"nestedStats": {"entries": {"monitorStatus": {"description": "up"}, "port": {"value": 80}}}}}}))
    df = client.execute_query({"table": "pool_member_stats"})
    paths = [urlsplit(r.url).path for r in http["gets"]()]
    assert paths == ["/mgmt/tm/ltm/pool", "/mgmt/tm/ltm/pool/~Common~p1/members/stats", "/mgmt/tm/ltm/pool/~App1~p2/members/stats"]
    assert parse_qs(urlsplit(http["gets"]()[0].url).query)["$select"] == ["fullPath"]
    assert list(df["pool"]) == ["/Common/p1", "/App1/p2"] and list(df["_object"]) == ["/Common/n:80"] * 2
    assert str(df["port"].dtype) == "Int64"


def test_secret_like_keys_are_stripped_but_identity_kept(client, http):
    http["queue"].append((200, _collection([{"fullPath": "/Common/m", "username": "probe", "password": "$M$sensitive", "nested": {"proxyCaPassphrase": "sensitive", "ok": 1}}])))
    df = client.execute_query({"table": "ltm_monitors_http"})
    assert "sensitive" not in df.to_json()
    assert df.iloc[0]["username"] == "probe" and "password" not in df.columns and "nested.proxyCaPassphrase" not in df.columns


def test_dtypes_follow_catalog_types(client, http):
    http["queue"].append((200, _collection([{"fullPath": "/Common/vs", "enabled": True, "connectionLimit": 0, "vsIndex": 7, "rules": ["/Common/r"]}, {"fullPath": "/Common/vs2", "disabled": True, "connectionLimit": None}])))
    df = client.execute_query({"table": "virtual_servers"})
    assert str(df["enabled"].dtype) == "boolean" and df["enabled"].tolist()[0] is True and pd_isna(df["enabled"].iloc[1])
    assert str(df["connectionLimit"].dtype) == "Int64" and str(df["fullPath"].dtype) == "string"
    assert df.iloc[0]["rules"] == ["/Common/r"] and df["rules"].dtype == object


def pd_isna(value):
    import pandas as pd

    return bool(pd.isna(value))


def test_connection_reports_version_and_self_device(client, http):
    http["queue"].extend([(200, _version()), (200, _collection([{"fullPath": "/Common/a", "hostname": "bigip-a", "failoverState": "active", "selfDevice": "true"}, {"fullPath": "/Common/b", "hostname": "bigip-b", "failoverState": "standby", "selfDevice": "false"}]))])
    result = client.test_connection()
    assert result["success"] and "17.1.1" in result["message"] and "bigip-a" in result["message"] and "26" in result["message"]
    http["queue"].append((200, _collection([{"fullPath": "/Common/p"}])))
    assert client.execute_query({"table": "pools"}).iloc[0]["_bow_device"] == "bigip-a"


def test_connection_rejects_pre_token_tmos(client, http):
    http["queue"].append((200, _version("11.6.1")))
    with pytest.raises(F5BigIpError) as e:
        client.test_connection()
    assert e.value.code == "UnsupportedVersion"


def test_connection_survives_device_read_permission_denied(client, http):
    http["queue"].extend([(200, _version("16.1.4")), (403, {"code": 403, "message": "x"})])
    assert client.test_connection()["success"]


def test_query_alias_and_async_wrapper_share_the_contract(client, http):
    import asyncio

    http["queue"].extend([(200, _collection([])), (200, _collection([]))])
    assert client.query({"table": "pools"}).empty
    assert asyncio.run(client.aexecute_query({"table": "pools"})).empty
    assert len(http["deleted"]) == 2


def test_rca_query_runs_through_bow_code_executor(client, http):
    from app.ai.code_execution.code_execution import StreamingCodeExecutor

    http["queue"].append((200, _stats("/mgmt/tm/ltm/pool", ["/Common/a", "/Common/b"], lambda o: {"activeMemberCnt": {"value": 0 if o.endswith("b") else 2}, "memberCnt": {"value": 2}})))
    code = """
def generate_df(ds_clients, excel_files):
    df = ds_clients["f5"].execute_query({"table": "pool_stats", "fields": ["_object", "activeMemberCnt", "memberCnt"]})
    return df[df["activeMemberCnt"] < df["memberCnt"]]
"""
    client._bow_connection_id = "synthetic-connection"
    frame, _, queries = StreamingCodeExecutor().execute_code(code=code, ds_clients={"f5": client}, excel_files=[])
    assert list(frame["_object"]) == ["/Common/b"] and set(frame["_bow_connection"]) == {"synthetic-connection"}
    assert len(queries) == 1 and json.loads(queries[0])["table"] == "pool_stats"


# ----------------------------------------------------------------------------- real HTTP against the simulator
@pytest.fixture
def simulator(monkeypatch):
    monkeypatch.setenv("F5_USERNAME", "reader")
    monkeypatch.setenv("F5_PASSWORD", "sim-secret-1")
    # Load under a unique module name: sibling connector suites ship their own
    # tools/<vendor>/simulated_api.py and must not collide in sys.modules.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "f5_bigip_simulated_api", Path(__file__).parents[3] / "tools/f5_bigip/simulated_api.py"
    )
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    sim.STATE["tokens"].clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), sim.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield sim, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_simulator_end_to_end_over_http(simulator):
    sim, url = simulator
    with pytest.raises(F5BigIpError) as denied:
        F5BigIpClient(url=url, username="reader", password="wrong", allow_http=True).test_connection()
    assert denied.value.code == "AuthenticationFailed"

    client = F5BigIpClient(url=url, username="reader", password="sim-secret-1", allow_http=True)
    result = client.test_connection()
    assert result["success"] and "17.1.1" in result["message"] and "active" in result["message"]
    for table in PRIMARY:
        frame = client.execute_query({"table": table})
        assert len(frame) > 0, table
        assert str(frame["_bow_retrieved_at"].dtype) == "datetime64[ns, UTC]"
    offline = client.execute_query({"table": "pool_member_stats", "filter": {"status.availabilityState": "offline"}})
    assert len(offline) >= 2 and set(offline["monitorStatus"]) <= {"down", "forced-down"}
    scoped = client.execute_query({"table": "pool_members", "parent": {"pool": "/Common/web_pool"}, "filter": {"state": "down"}})
    assert list(scoped["fullPath"]) == ["/Common/web03:80"] and list(scoped["pool"]) == ["/Common/web_pool"]
    app1 = client.execute_query({"table": "virtual_servers", "filter": {"partition": "App1"}, "fields": ["fullPath", "pool", "enabled", "disabled"]})
    assert set(app1["fullPath"]) == {"/App1/vs_app1_web", "/App1/vs_app1_cache"}
    certs = client.execute_query({"table": "ssl_certs", "fields": ["fullPath", "expirationDate"]})
    now = certs["_bow_retrieved_at"].iloc[0].timestamp()
    expired = certs[certs["expirationDate"] < now]
    assert list(expired["fullPath"]) == ["/Common/legacy-api.crt"]
    with pytest.raises(F5BigIpError) as missing:
        client.execute_query({"table": "pools", "name": "/Common/nope"})
    assert missing.value.code == "UnsupportedResource" and missing.value.f5_code == 404
    with pytest.raises(F5BigIpError):
        client.execute_query({"table": "pools", "limit": 2})
    assert not sim.STATE["tokens"], "every issued token must be deleted at the end of a query session"


def test_simulator_token_expiry_mid_query_is_recovered_by_relogin(simulator):
    sim, url = simulator
    calls = {"gets": 0, "logins": 0}
    original_get, original_login = sim.Handler.do_GET, sim.Handler.login

    def expiring_get(handler):
        calls["gets"] += 1
        if calls["gets"] == 2:
            with sim.STATE["lock"]:
                for entry in sim.STATE["tokens"].values():
                    entry["expires"] = 0  # the BIG-IP lets the token lapse between two pages
        return original_get(handler)

    def counting_login(handler):
        calls["logins"] += 1
        return original_login(handler)

    sim.Handler.do_GET, sim.Handler.login = expiring_get, counting_login
    try:
        client = F5BigIpClient(url=url, username="reader", password="sim-secret-1", allow_http=True)
        frame = client.execute_query({"table": "pool_member_stats"})
    finally:
        sim.Handler.do_GET, sim.Handler.login = original_get, original_login
    assert len(frame) == 15 and frame["pool"].nunique() == 6
    assert calls["logins"] == 2
    assert not sim.STATE["tokens"]
