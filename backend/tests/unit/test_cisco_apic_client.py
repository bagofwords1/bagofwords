"""Cisco APIC contracts exercised at the HTTP boundary; no controller credentials.

Most tests run the real client against the in-repo APIC simulator over loopback
HTTP (tools/cisco_apic/simulated_api.py), which enforces the APIC wire contract:
login/logout, signature verification, query-target-filter, paging, error
envelopes, read-only refusals and the admin-read RBAC blind spot. Transport
failures that a simulator cannot produce (dead controllers, a busy cluster) are
driven by replacing `requests.Session.send`. Nothing asserts on private helpers.
"""

import base64
import importlib.util
import io
import json
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import pytest
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.data_sources.clients.cisco_apic_client import ApicQueryError, CiscoApicClient
from app.schemas.data_source_registry import REGISTRY, resolve_client_class

USER, PASSWORD = "bow-ro", "sim-only-password"
LIMITED, LIMITED_PASSWORD = "bow-readall", "sim-only-limited"
CERT_USER, CERT_NAME = "bow-cert", "bow-signing"
CATALOG = json.loads((Path(__file__).parents[2] / "app/data_sources/clients/cisco_apic/catalog.json").read_text())
_SIM = Path(__file__).parents[3] / "tools/cisco_apic/simulated_api.py"


def _load_simulator():
    if "cisco_apic_simulated_api" in sys.modules:
        return sys.modules["cisco_apic_simulated_api"]
    spec = importlib.util.spec_from_file_location("cisco_apic_simulated_api", _SIM)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cisco_apic_simulated_api"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def private_pem(signing_key):
    return signing_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


@pytest.fixture(scope="module")
def apic(signing_key, tmp_path_factory):
    """A running simulator: (base url, Simulator instance)."""
    from http.server import ThreadingHTTPServer

    public = tmp_path_factory.mktemp("apic") / "user-cert.pem"
    public.write_bytes(signing_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    mp = pytest.MonkeyPatch()
    for k, v in {
        "APIC_USERNAME": USER, "APIC_PASSWORD": PASSWORD, "APIC_LIMITED_USERNAME": LIMITED, "APIC_LIMITED_PASSWORD": LIMITED_PASSWORD,
        "APIC_CERT_USERNAME": CERT_USER, "APIC_CERT_NAME": CERT_NAME, "APIC_CERT_PEM": str(public),
    }.items():
        mp.setenv(k, v)
    mock = _load_simulator()
    sim = mock.Simulator()
    server = ThreadingHTTPServer(("127.0.0.1", 0), mock.make_handler(sim))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", sim
    server.shutdown()
    mp.undo()


@pytest.fixture
def sim(apic):
    _, s = apic
    s.log.clear()
    s.fail_next.clear()
    yield s
    # Invariants for every test: read-only and no leaked sessions.
    assert s.writes == [] and s.subscriptions == []
    assert s.sessions == {}, "every password session must be logged out"
    assert not any("aaaRefresh" in uri for _, uri in s.log)


@pytest.fixture
def client(apic, sim):
    c = CiscoApicClient(url=apic[0], username=USER, password=PASSWORD, allow_http=True)
    yield c
    c.close()


@pytest.fixture
def cert_client(apic, sim, private_pem):
    c = CiscoApicClient(url=apic[0], username=CERT_USER, private_key=private_pem, certificate_name=CERT_NAME, allow_http=True)
    yield c
    c.close()


def query_params(uri):
    return {k: v[0] for k, v in parse_qs(urlsplit(uri).query).items()}


# ----------------------------------------------------------------------------- registry & schema
def test_registry_entry_resolves_client_with_both_auth_variants(private_pem):
    entry = REGISTRY["cisco_apic"]
    assert resolve_client_class("cisco_apic") is CiscoApicClient
    assert entry.category == "infra" and entry.client_path.endswith("CiscoApicClient")
    assert set(entry.credentials_auth.by_auth) == {"certificate", "userpass"}
    assert entry.credentials_auth.default == "certificate"
    assert entry.credentials_auth.by_auth["certificate"].scopes == ["system"]
    assert "user" in entry.credentials_auth.by_auth["userpass"].scopes
    config = entry.config_schema(url="https://apic1.example.com")
    for creds in (
        entry.credentials_auth.by_auth["userpass"].schema(username="reader", password="x"),
        entry.credentials_auth.by_auth["certificate"].schema(username="reader", certificate_name="bow", private_key=private_pem),
    ):
        c = CiscoApicClient(**config.model_dump(), **creds.model_dump())
        assert len(c.get_tables()) == len(CATALOG["tables"])


def test_every_table_exposes_class_properties_derived_columns_and_provenance(client):
    tables = {t.name: t for t in client.get_tables()}
    assert {"fabric_nodes", "faults", "events", "audit_log", "interface_status", "bgp_peers", "endpoints", "filter_entries", "switch_firmware"} <= tables.keys()
    for name, table in tables.items():
        names = [c.name for c in table.columns]
        assert "dn" in names and {"pod", "node", "parent_dn"} <= set(names) and "_bow_retrieved_at" in names
        assert len(names) == len(set(names)), name
        assert table.metadata_json["cisco_apic"]["class"] == CATALOG["tables"][name]["class"]
    assert "Needs admin read access" in tables["faults"].description
    assert "Supports node" in tables["interface_status"].description
    assert len(client.prompt_schema()) < 15000 and "audit_log" in client.description


def test_secure_model_properties_are_never_exposed():
    for name, table in CATALOG["tables"].items():
        assert not any(k.lower() in {"password", "pwd", "key", "passphrase"} for k in table["fields"]), name


# ----------------------------------------------------------------------------- configuration
@pytest.mark.parametrize(
    "kwargs",
    [
        {"url": "http://apic.example.com", "password": "x"},  # http outside loopback
        {"url": "https://apic.example.com/api", "password": "x"},  # path
        {"url": "https://user:pw@apic.example.com", "password": "x"},  # credentials in url
        {"url": "https://apic.example.com"},  # no secret
        {"url": "https://apic.example.com", "password": "x", "private_key": "PEM"},  # both secrets
        {"url": "https://apic.example.com", "private_key": "not a pem", "certificate_name": "c"},
        {"url": "https://apic.example.com", "password": "x", "login_domain": "bad domain!"},
        {"url": "https://apic.example.com", "password": "x", "fallback_urls": "https://a,https://b,https://c,https://d,https://e"},
        {"url": "https://apic.example.com", "password": "x", "username": "a b"},
    ],
)
def test_invalid_configuration_is_rejected_at_construction(kwargs):
    kwargs = {"username": "reader", **kwargs}
    with pytest.raises(ApicQueryError) as e:
        CiscoApicClient(**kwargs)
    assert e.value.code == "InvalidConfiguration"
    assert "x" not in str(e.value).split(":", 1)[0]


def test_certificate_auth_rejects_small_keys_login_domains_and_missing_certificate_name(private_pem):
    small = rsa.generate_private_key(public_exponent=65537, key_size=1024).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    for kwargs in (
        {"private_key": small, "certificate_name": "c"},
        {"private_key": private_pem, "certificate_name": ""},
        {"private_key": private_pem, "certificate_name": "c", "login_domain": "corp-ad"},
    ):
        with pytest.raises(ApicQueryError) as e:
            CiscoApicClient(url="https://apic.example.com", username="reader", **kwargs)
        assert e.value.code == "InvalidConfiguration"


def test_empty_optional_form_values_are_accepted():
    c = CiscoApicClient(url="https://apic.example.com", username="reader", password="x", private_key="", certificate_name="",
                        login_domain="", fallback_urls="", ca_certificate="", timeout="30", max_rows="500")
    assert c.max_rows == 500 and c.timeout == 30


# ----------------------------------------------------------------------------- authentication
def test_password_session_logs_in_queries_and_logs_out(client, sim):
    client.execute_query({"table": "tenants"})
    methods = [(m, urlsplit(u).path) for m, u in sim.log]
    assert methods[0] == ("POST", "/api/aaaLogin.json") and methods[-1] == ("POST", "/api/aaaLogout.json")
    assert all(m == "GET" for m, _ in methods[1:-1]) and len(methods) == 3


def test_remote_users_log_in_through_their_login_domain(apic, sim):
    c = CiscoApicClient(url=apic[0], username=USER, password=PASSWORD, login_domain="corp-ad", allow_http=True)
    assert len(c.execute_query({"table": "tenants"})) == 5
    with pytest.raises(ApicQueryError) as e:
        CiscoApicClient(url=apic[0], username=USER, password=PASSWORD, login_domain="unknown-dom", allow_http=True).execute_query({"table": "tenants"})
    assert e.value.code == "AuthenticationFailed"


def test_bad_password_is_authentication_failed_without_echo(apic, sim):
    c = CiscoApicClient(url=apic[0], username=USER, password="wrong-secret-value", allow_http=True)
    with pytest.raises(ApicQueryError) as e:
        c.test_connection()
    assert e.value.code == "AuthenticationFailed" and "wrong-secret-value" not in str(e.value)
    assert [urlsplit(u).path for _, u in sim.log] == ["/api/aaaLogin.json"]


def test_certificate_auth_signs_every_request_and_opens_no_session(cert_client, sim, signing_key, monkeypatch):
    seen = []
    original = requests.Session.send

    def capture(session, request, **kwargs):
        seen.append(request)
        return original(session, request, **kwargs)

    monkeypatch.setattr(requests.Session, "send", capture)
    logins = sim.logins
    frame = cert_client.execute_query({"table": "fabric_nodes"})
    assert len(frame) == 9 and sim.logins == logins and seen
    assert all(urlsplit(r.url).path not in {"/api/aaaLogin.json", "/api/aaaLogout.json"} for r in seen)
    for request in seen:
        cookies = dict(part.strip().split("=", 1) for part in request.headers["Cookie"].split(";"))
        assert cookies["APIC-Certificate-DN"] == f"uni/userext/user-{CERT_USER}/usercert-{CERT_NAME}"
        assert cookies["APIC-Certificate-Algorithm"] == "v1.0" and cookies["APIC-Certificate-Fingerprint"] == "fingerprint"
        uri = request.path_url
        signing_key.public_key().verify(base64.b64decode(cookies["APIC-Request-Signature"]), ("GET" + uri).encode(), padding.PKCS1v15(), hashes.SHA256())


def test_a_signature_from_another_key_is_refused(apic, sim):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    c = CiscoApicClient(url=apic[0], username=CERT_USER, private_key=other, certificate_name=CERT_NAME, allow_http=True)
    with pytest.raises(ApicQueryError) as e:
        c.execute_query({"table": "tenants"})
    assert e.value.code == "PermissionDenied"


# ----------------------------------------------------------------------------- connection test
def test_test_connection_reports_version_and_inventory(client, cert_client):
    for c in (client, cert_client):
        result = c.test_connection()
        assert result["success"] and "6.2(1g)" in result["message"] and "3 controllers and 6 switches" in result["message"]
        assert "admin read" not in result["message"]


def test_read_all_account_is_warned_that_faults_are_invisible(apic, sim):
    c = CiscoApicClient(url=apic[0], username=LIMITED, password=LIMITED_PASSWORD, allow_http=True)
    result = c.test_connection()
    assert result["success"] and "lacks admin read access" in result["message"]
    assert c.execute_query({"table": "faults"}).empty and len(c.execute_query({"table": "fabric_nodes"})) == 9
    assert c.get_schema("faults").metadata_json["cisco_apic"]["availability"] == "empty_or_admin_only"


# ----------------------------------------------------------------------------- read-only guarantee
@pytest.mark.parametrize(
    "method,uri",
    [
        ("POST", "/api/mo/uni/tn-retail.json"),
        ("DELETE", "/api/mo/uni/tn-retail.json"),
        ("GET", "/api/aaaRefresh.json"),
        ("GET", "/api/mo/uni/tn-retail.json"),
        ("GET", "/api/class/aaaUser.json"),
        ("GET", "/api/class/faultInst.json?subscription=yes"),
        ("GET", "/api/class/faultInst.json?rsp-subtree=full"),
        ("GET", "/api/node/class/topology/pod-1/node-1/../faultInst.json"),
        ("GET", "/files/techsupport.tgz"),
        ("POST", "/api/aaaLogin.json?x=1"),
    ],
)
def test_client_cannot_build_a_request_outside_the_read_allow_list(client, cert_client, sim, method, uri):
    for c in (client, cert_client):
        # The network primitive itself is the guarantee under test.
        with c._session(c._urls[0]) as session, pytest.raises(ApicQueryError) as e:  # noqa: SLF001
            c._request(session, method, uri, 10**12)  # noqa: SLF001
        assert e.value.code == "ForbiddenOperation"
    assert all(urlsplit(u).path in {"/api/aaaLogin.json", "/api/aaaLogout.json"} for _, u in sim.log)


@pytest.mark.parametrize(
    "query",
    [
        {"table": "nope"},
        {"table": "faults", "url": "/api/mo/uni.json"},
        {"table": "faults", "fields": ["password"]},
        {"table": "faults", "filter": {"pwd": "x"}},
        {"table": "faults", "filter": {"descr": 'x") or eq(faultInst.code,"F1'}},
        {"table": "faults", "filter": {"descr": "a\\b"}},
        {"table": "faults", "filter": {"severity": {"op": "regex", "value": ".*"}}},
        {"table": "faults", "filter": {"severity": {"op": "in", "value": []}}},
        {"table": "faults", "filter": {"occur": "many"}},
        {"table": "faults", "filter": {"created": {"op": "gt", "value": "yesterday"}}},
        {"table": "faults", "lookback": "forever"},
        {"table": "faults", "lookback": "24h", "start_time": "2026-01-01T00:00:00Z"},
        {"table": "events", "lookback": "24h", "filter": {"created": {"op": "gt", "value": "2026-01-01T00:00:00Z"}}},
        {"table": "tenants", "lookback": "24h"},
        {"table": "tenants", "node": 301},
        {"table": "faults", "node": "301; drop"},
        {"table": "faults", "node": list(range(1, 30))},
        {"table": "faults", "pod": 1},
        {"table": "faults", "sort": ["dn sideways"]},
        {"table": "faults", "limit": 0},
        {"table": "faults", "filter": {"descr": {"op": "in", "value": [str(i) for i in range(21)]}}},
        "not json",
    ],
)
def test_invalid_or_unsafe_queries_send_nothing(client, sim, query):
    with pytest.raises(ApicQueryError) as e:
        client.execute_query(query)
    assert e.value.code == "InvalidQuery"
    assert sim.log == []


# ----------------------------------------------------------------------------- query translation
def test_sql_text_gets_a_json_example_instead_of_running(client, sim):
    with pytest.raises(ApicQueryError) as e:
        client.execute_query("SELECT 1 AS example")
    assert e.value.code == "InvalidQuery" and '{"table": "faults"' in str(e.value) and sim.log == []


def test_filters_compile_to_apic_query_target_filter(client, sim):
    client.execute_query({"table": "faults", "filter": {"severity": {"op": "in", "value": ["critical", "major"]}, "code": "F1394", "occur": {"op": "ge", "value": 1}}})
    (_, uri), = [x for x in sim.log if x[0] == "GET"]
    params = query_params(uri)
    assert params["query-target-filter"] == 'and(or(eq(faultInst.severity,"critical"),eq(faultInst.severity,"major")),eq(faultInst.code,"F1394"),ge(faultInst.occur,"1"))'
    assert params["order-by"] == "faultInst.lastTransition|desc" and params["page"] == "0" and int(params["page-size"]) <= 500


def test_contains_matches_literally_even_with_regex_and_dn_brackets(client, sim):
    frame = client.execute_query({"table": "interface_status", "filter": {"dn": {"op": "contains", "value": "phys-[eth1/50]"}}})
    assert set(frame["node"]) == {301, 302, 303, 304} and all("phys-[eth1/50]" in dn for dn in frame["dn"])
    assert client.execute_query({"table": "endpoints", "filter": {"ip": {"op": "contains", "value": "10.10.3"}}}).ip.str.contains("10.10.3", regex=False).all()
    # "." must not act as a wildcard: 10x10 matches nothing.
    assert client.execute_query({"table": "endpoints", "filter": {"ip": {"op": "contains", "value": "10x10"}}}).empty


def test_lookback_becomes_a_utc_window_on_the_time_column(client, sim):
    frame = client.execute_query({"table": "audit_log", "lookback": "2h"})
    (_, uri), = [x for x in sim.log if x[0] == "GET"]
    flt = query_params(uri)["query-target-filter"]
    assert flt.startswith('and(ge(aaaModLR.created,"') and 'le(aaaModLR.created,"' in flt and flt.count("+00:00") == 2
    assert not frame.empty and (frame["created"] >= frame["_bow_window_start"]).all()
    assert (frame["_bow_window_end"] - frame["_bow_window_start"] == timedelta(hours=2)).all()


def test_record_tables_always_apply_a_window(client, sim):
    events = client.execute_query({"table": "events", "limit": 2000})
    assert not events.empty
    assert (events["created"] >= pd.Timestamp.now(tz="UTC") - timedelta(hours=24, minutes=1)).all()
    older = client.execute_query({"table": "events", "lookback": "14d", "filter": {"cause": "node-rebooted"}})
    assert len(older) == 1  # the 12-day-old reboot is only visible with a wider window


def test_node_scoping_reads_only_that_switch(client, sim):
    frame = client.execute_query({"table": "interface_status", "node": [303, 304], "filter": {"operSt": "up"}})
    assert set(frame["node"]) == {303, 304}
    paths = {urlsplit(u).path for m, u in sim.log if m == "GET"}
    assert paths == {"/api/node/class/topology/pod-1/node-303/ethpmPhysIf.json", "/api/node/class/topology/pod-1/node-304/ethpmPhysIf.json"}


def test_paging_returns_every_object_exactly_once(client, sim):
    frame = client.execute_query({"table": "fabric_health_history", "lookback": "4d", "fields": ["dn", "healthAvg"], "limit": 2000})
    gets = [u for m, u in sim.log if m == "GET"]
    assert len(gets) >= 2 and frame["dn"].is_unique and len(frame) == len(sim.mo["fabricOverallHealthHist5min"])
    assert [query_params(u)["page"] for u in gets] == [str(i) for i in range(len(gets))]


def test_limit_is_a_complete_result_ceiling_never_a_truncation(client, sim):
    with pytest.raises(ApicQueryError) as e:
        client.execute_query({"table": "interface_status", "limit": 50})
    assert e.value.code == "ResultLimitExceeded" and "208" in str(e.value)
    assert len([u for m, u in sim.log if m == "GET"]) == 1  # fails on the first page's totalCount


def test_sort_and_field_projection(client, sim):
    frame = client.execute_query({"table": "fabric_nodes", "fields": ["id", "name"], "sort": ["id desc"]})
    assert list(frame["id"]) == sorted(frame["id"], reverse=True)
    assert [c for c in frame.columns if not c.startswith("_bow_")] == ["dn", "id", "name", "pod", "node", "parent_dn"]
    assert len(client.execute_query({"table": "fabric_nodes", "fields": "*"}).columns) > 20


# ----------------------------------------------------------------------------- results
def test_types_times_and_derived_columns(client, sim):
    faults = client.execute_query({"table": "faults", "filter": {"code": "F1394"}})
    assert str(faults["created"].dtype) == "datetime64[ns, UTC]" and str(faults["occur"].dtype) == "Int64"
    raw = next(o for o in sim.mo["faultInst"] if o["code"] == "F1394")
    assert raw["created"].endswith("+02:00")
    assert pd.Timestamp(datetime.fromisoformat(raw["created"])).tz_convert("UTC") in set(faults["created"])
    leaf = faults[faults["node"] == 303].iloc[0]
    assert leaf["pod"] == 1 and leaf["parent_dn"] == "topology/pod-1/node-303/sys/phys-[eth1/50]/phys"
    peers = client.execute_query({"table": "bgp_peers", "node": 304, "fields": ["addr", "updateElapsedTs"]})
    assert peers.set_index("addr")["updateElapsedTs"].isna().to_dict() == {"10.200.0.5": True}  # the APIC's 1970 "never" becomes null
    assert set(client.execute_query({"table": "tenants"})["parent_dn"]) == {"uni"}


def test_rca_story_is_answerable_from_the_tables(client, sim):
    changes = client.execute_query({"table": "audit_log", "lookback": "1h"})
    change = changes[changes["affected"].str.contains("flt-postgres")].iloc[0]
    assert change["user"] == "netops-jlee" and "5433" in change["changeSet"]
    assert client.execute_query({"table": "filter_entries", "filter": {"name": "pg-5432"}}).iloc[0]["dToPort"] == "5433"
    db = client.execute_query({"table": "endpoints", "filter": {"ip": "10.10.30.21"}}).iloc[0]
    assert db["parent_dn"] == "uni/tn-retail/ap-checkout/epg-db" and "protpaths-303-304" in db["fabricPathDn"]
    down = client.execute_query({"table": "interface_status", "filter": {"operSt": "down", "usage": "fabric"}})
    assert set(down["operStQual"]) == {"sfp-missing", "link-failure"}
    peers = client.execute_query({"table": "bgp_peers", "filter": {"operSt": {"op": "ne", "value": "established"}}})
    assert list(peers["addr"]) == ["10.200.0.5"] and peers.iloc[0]["connDrop"] == 14
    versions = client.execute_query({"table": "switch_firmware"})
    assert versions["version"].nunique() == 2


def test_runs_through_bow_code_executor(client):
    from app.ai.code_execution.code_execution import StreamingCodeExecutor

    code = """
def generate_df(ds_clients, excel_files):
    df = ds_clients["aci"].execute_query({"table": "faults", "filter": {"severity": {"op": "in", "value": ["critical", "major"]}}})
    return df[["code", "severity", "node", "parent_dn"]]
"""
    client._bow_connection_id = "synthetic-connection"
    frame, _, queries = StreamingCodeExecutor().execute_code(code=code, ds_clients={"aci": client}, excel_files=[])
    assert set(frame["severity"]) == {"major"} and len(queries) == 1 and json.loads(queries[0])["table"] == "faults"


def test_query_alias_and_async_wrapper_share_the_contract(client):
    import asyncio

    assert len(client.query({"table": "tenants"})) == 5
    assert len(asyncio.run(client.aexecute_query({"table": "tenants"}))) == 5


# ----------------------------------------------------------------------------- errors, retries, failover
def test_apic_rejections_surface_the_apic_reason(client, sim, monkeypatch):
    from app.data_sources.clients import cisco_apic_client as module

    catalog = json.loads(json.dumps(module._catalog()))  # noqa: SLF001
    catalog["tables"]["tenants"]["fields"]["notAProp"] = {"type": "string", "label": "x"}
    monkeypatch.setattr(module, "_catalog", lambda: catalog)
    with pytest.raises(ApicQueryError) as e:
        client.execute_query({"table": "tenants", "filter": {"notAProp": "x"}})
    assert e.value.code == "InvalidQuery" and e.value.status == 400 and "Unknown property notAProp" in str(e.value)


def test_busy_apic_is_retried_then_returns_complete_data(client, sim, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    sim.fail_next.extend([503, 503])
    assert len(client.execute_query({"table": "tenants"})) == 5


def test_failover_to_the_next_controller_when_the_first_is_down(apic, sim, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    c = CiscoApicClient(url="http://127.0.0.1:9", fallback_urls=apic[0], username=USER, password=PASSWORD, allow_http=True)
    assert len(c.execute_query({"table": "tenants"})) == 5
    assert "connected to apic" in c.test_connection()["message"].lower()


def test_unreachable_cluster_is_transport_failed(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    def refuse(session, request, **kwargs):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(requests.Session, "send", refuse)
    c = CiscoApicClient(url="https://apic1.example.com", fallback_urls="https://apic2.example.com", username="r", password="x")
    with pytest.raises(ApicQueryError) as e:
        c.execute_query({"table": "tenants"})
    assert e.value.code == "TransportFailed"


@pytest.mark.parametrize("status,code", [(403, "PermissionDenied"), (500, "HttpError"), (404, "HttpError")])
def test_http_errors_map_to_codes(monkeypatch, status, code):
    def send(session, request, **kwargs):
        r = requests.Response()
        r.url, r.status_code = request.url, status
        r.raw = io.BytesIO(json.dumps({"totalCount": "1", "imdata": [{"error": {"attributes": {"code": str(status), "text": "nope"}}}]}).encode())
        return r

    monkeypatch.setattr(requests.Session, "send", send)
    c = CiscoApicClient(url="https://apic1.example.com", username="r", private_key=rsa.generate_private_key(65537, 2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(), certificate_name="c")
    with pytest.raises(ApicQueryError) as e:
        c.execute_query({"table": "tenants"})
    assert e.value.code == code


def test_error_envelope_with_http_200_is_not_data(monkeypatch):
    def send(session, request, **kwargs):
        r = requests.Response()
        r.url, r.status_code = request.url, 200
        r.raw = io.BytesIO(json.dumps({"totalCount": "1", "imdata": [{"error": {"attributes": {"code": "122", "text": "unknown"}}}]}).encode())
        return r

    monkeypatch.setattr(requests.Session, "send", send)
    c = CiscoApicClient(url="https://apic1.example.com", username="r", private_key=rsa.generate_private_key(65537, 2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(), certificate_name="c")
    with pytest.raises(ApicQueryError) as e:
        c.execute_query({"table": "tenants"})
    assert e.value.apic_code == "122"


def test_every_table_reads_from_the_simulator(client, sim):
    for name in CATALOG["tables"]:
        frame = client.execute_query({"table": name, "limit": 5000})
        assert len(frame) > 0, name
        assert frame["dn"].notna().all() and frame["dn"].is_unique, name


# ----------------------------------------------------------------------------- planner ergonomics (found in a live agent run)
def test_derived_columns_can_be_filtered_like_properties(client, sim):
    full = client.execute_query({"table": "interface_status", "limit": 1000})
    for flt, expected in (
        ({"node": 303}, full[full["node"] == 303]),
        ({"node": {"op": "in", "value": [301, 304]}}, full[full["node"].isin([301, 304])]),
        ({"node": {"op": "ne", "value": 303}}, full[full["node"] != 303]),
        ({"pod": 1}, full[full["pod"] == 1]),
        ({"parent_dn": "topology/pod-1/node-303/sys/phys-[eth1/50]"}, full[full["parent_dn"] == "topology/pod-1/node-303/sys/phys-[eth1/50]"]),
        ({"parent_dn": {"op": "contains", "value": "phys-[eth1/5"}}, full[full["parent_dn"].str.contains("phys-[eth1/5", regex=False)]),
    ):
        got = client.execute_query({"table": "interface_status", "filter": flt, "limit": 1000})
        assert sorted(got["dn"]) == sorted(expected["dn"]) and len(got) > 0, flt


def test_parent_dn_filter_finds_the_children_of_one_object(client, sim):
    provided = client.execute_query({"table": "epg_provided_contracts", "filter": {"parent_dn": "uni/tn-retail/ap-checkout/epg-db"}})
    assert list(provided["tnVzBrCPName"]) == ["app-to-db"]
    with pytest.raises(ApicQueryError) as e:
        client.execute_query({"table": "epgs", "filter": {"parent_dn": {"op": "gt", "value": "x"}}})
    assert e.value.code == "InvalidQuery"


@pytest.mark.parametrize(
    "query,suggested",
    [
        ({"table": "epgs", "fields": ["dn", "tenantId"]}, None),
        ({"table": "interface_status", "filter": {"operst": "down"}}, "operSt"),
        ({"table": "faults", "sort": ["serverity desc"]}, "severity"),
    ],
)
def test_unknown_property_errors_name_the_valid_properties(client, sim, query, suggested):
    with pytest.raises(ApicQueryError) as e:
        client.execute_query(query)
    message = str(e.value)
    assert e.value.code == "InvalidQuery" and "Main properties:" in message and "parent_dn" in message
    assert suggested is None or f"Did you mean {suggested}" in message
    assert sim.log == []


def test_limit_error_says_limit_is_not_a_preview_size(client, sim):
    with pytest.raises(ApicQueryError) as e:
        client.execute_query({"table": "events", "lookback": "2h", "limit": 3})
    assert e.value.code == "ResultLimitExceeded" and "limit is 3" in str(e.value) and "drop limit" in str(e.value)


def test_every_model_property_is_filterable_even_when_unset(client, sim):
    # baseEpgDn is a real fvCEp property that the simulated endpoints leave unset.
    assert client.execute_query({"table": "endpoints", "filter": {"baseEpgDn": "uni/tn-retail/ap-checkout/epg-db"}}).empty
