"""Every connector built on ``requests`` connects through ``app.core.egress``.

The contract: the outbound policy applies to every connection a running
connector opens, including after a name's answer changes or a server
redirects, whichever way the connector holds its HTTP client.

Connectors are grouped below by how they make requests. Each row runs the real
client's own ``test_connection`` (or discovery) over real sockets, with the
network played by ``NetTrap``. The first probe must reach the local server
where one is possible, so a connector that fails early cannot pass by never
touching the network. Rows with a fixed name (a vendor's constant host, or a
connector that refuses plain HTTP) cannot use the local server, so they have
only the second half.
"""
from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest
import requests

from app.core import egress
from app.core.egress import PinnedHTTPAdapter, pinned_request, pinned_session
from tests.utils.egress_fakes import NetTrap, local_server

NAME = "conn.corp.invalid"
CLIENTS_DIR = Path(egress.__file__).parents[1] / "data_sources" / "clients"
METADATA_URL = "http://169.254.169.254/latest/user-data"


class _Anything(dict):
    """Any path -> 200 with an empty JSON object, or a redirect once switched."""

    redirect = False

    def get(self, key, default=None):
        if self.redirect:
            return (302, {"Location": METADATA_URL}, b"")
        return (200, {}, {})


def _open_rest_session(client):
    """vCenter's inventory goes through pyVmomi; its REST API is the ``requests`` part."""
    with client._rest():
        pass


def _azure_cost_request(client):
    """Token acquisition belongs to azure-identity; the API calls are the ``requests`` part."""
    client._session().get("https://management.azure.com/subscriptions", timeout=5)


def _client(module, cls):
    def build(**kwargs):
        mod = __import__(f"app.data_sources.clients.{module}", fromlist=[cls])
        return getattr(mod, cls)(**kwargs)
    return build


def _https(url):
    """For connectors that refuse plain HTTP; the local server speaks HTTP, so
    these rows have no first half either."""
    return url.replace("http://", "https://")


# (id, how to build it from the server URL, optional probe, fixed host name)
CASES = [
    # A session opened per operation.
    ("prometheus", lambda u: _client("prometheus_client", "PrometheusClient")(base_url=u)),
    ("jaeger", lambda u: _client("jaeger_client", "JaegerClient")(base_url=u)),
    ("zabbix", lambda u: _client("zabbix_client", "ZabbixClient")(url=u, api_token="t")),
    ("servicenow", lambda u: _client("servicenow_client", "ServiceNowClient")(instance_url=u, access_token="t")),
    ("posthog", lambda u: _client("posthog_client", "PostHogClient")(api_key="k", host=u, project_id="1")),
    ("priority_erp", lambda u: _client("priority_erp_client", "PriorityErpClient")(service_root=u + "/odata/Priority/t.ini/c", access_token="t")),
    ("crowdstrike", lambda u: _client("crowdstrike_client", "CrowdStrikeClient")(client_id="i", client_secret="s", base_url=u)),
    ("tableau", lambda u: _client("tableau_client", "TableauClient")(server_url=u, pat_name="n", pat_token="t")),
    # A session held on the client between calls.
    ("aria_operations", lambda u: _client("aria_operations_client", "AriaOperationsClient")(url=u, username="u", password="p")),
    ("dremio", lambda u: _client("dremio_client", "DremioClient")(url=u, token="t")),
    ("appdynamics", lambda u: _client("appdynamics_client", "AppDynamicsClient")(controller_url=u, username="u", password="p", account_name="a")),
    ("spectrum", lambda u: _client("spectrum_client", "SpectrumClient")(url=u, username="u", password="p")),
    ("timbr", lambda u: _client("timbr_client", "TimbrClient")(host=u, ontology="o", api_key="k")),
    ("timbr_a2a", lambda u: _client("timbr_a2a_client", "TimbrA2aClient")(host=u, ontology="o", api_key="k")),
    ("businessobjects", lambda u: _client("businessobjects_client", "BusinessObjectsClient")(host=u, logon_token="t")),
    ("sap_datasphere", lambda u: _client("sap_datasphere_client", "SapDatasphereClient")(host=u, access_token="t")),
    # One-shot requests.
    ("elasticsearch", lambda u: _client("elasticsearch_client", "ElasticsearchClient")(host=u.split("://")[1].split(":")[0], port=int(u.rsplit(":", 1)[1]), secure=False, user="u", password="p")),
    ("opensearch", lambda u: _client("opensearch_client", "OpenSearchClient")(host=u.split("://")[1].split(":")[0], port=int(u.rsplit(":", 1)[1]), secure=False, user="u", password="p")),
    ("splunk", lambda u: _client("splunk_client", "SplunkClient")(host=u, api_token="t")),
    ("druid", lambda u: _client("druid_client", "DruidClient")(host=u.split("://")[1].split(":")[0], port=int(u.rsplit(":", 1)[1]), basic_token="t", secure=False)),
    ("pinot", lambda u: _client("pinot_client", "PinotClient")(host="broker.invalid", port=1, secure=False, controller=u), lambda c: c.get_tables()),
    # A token exchange ahead of the session.
    ("sisense", lambda u: _client("sisense_client", "SisenseClient")(host=u, username="u", password="p")),
    ("qlik_sense", lambda u: _client("qlik_sense_client", "QlikSenseClient")(base_url=u, client_id="i", client_secret="s")),
    ("infor_epm", lambda u: _client("infor_epm_client", "InforEpmClient")(api_url=u, olap_database="d", gateway_token_url=u + "/token", gateway_client_id="i", gateway_client_secret="s")),
    # simple_salesforce always speaks HTTPS to the instance host.
    ("salesforce", lambda u: _client("salesforce_client", "SalesforceClient")(access_token="t", instance_url=_https(u)), None, NAME),
    # XML over HTTP.
    ("analysis_services", lambda u: _client("analysis_services_client", "AnalysisServicesClient")(host=u, username="u", password="p")),
    ("sap_bw_xmla", lambda u: _client("sap_bw_xmla_client", "SapBwXmlaClient")(host=u, username="u", password="p")),
    ("infor_olap", lambda u: _client("infor_olap_client", "InforOlapClient")(host=u, username="u", password="p")),
    ("oracle_bi", lambda u: _client("oracle_bi_client", "OracleBIClient")(host=u, username="u", password="p")),
    # Windows auth, no ambient environment, a session per thread.
    ("mecm", lambda u: _client("mecm_client", "MecmClient")(server_url=_https(u), username="u", password="p"), None, NAME),
    ("sharepoint_onprem", lambda u: _client("sharepoint_onprem_client", "SharepointOnpremClient")(site_url=u, username="u", password="p", allow_http=True)),
    ("powerbi_report_server", lambda u: _client("powerbi_report_server_client", "PowerBIReportServerClient")(server_url=u, username="u", password="p")),
    # Deployment-owned trust, no ambient environment.
    ("netapp_ontap", lambda u: _client("netapp_ontap_client", "NetAppOntapClient")(url=u, username="u", password="p", allow_http=True)),
    ("documentum", lambda u: _client("documentum_client", "DocumentumClient")(rest_url=u, repository="r", username="u", password="p", allow_http=True)),
    ("kms_lighthouse", lambda u: _client("kms_lighthouse_client", "KmsLighthouseClient")(base_url=u, username="u", password="p", allow_http=True)),
    ("brocade", lambda u: _client("brocade_client", "BrocadeClient")(url=_https(u), username="u", password="p"), None, NAME),
    ("checkpoint", lambda u: _client("checkpoint_client", "CheckPointClient")(url=_https(u), username="u", password="p"), None, NAME),
    ("dell_ome", lambda u: _client("dell_ome_client", "DellOmeClient")(url=_https(u), username="u", password="p"), None, NAME),
    ("f5_bigip", lambda u: _client("f5_bigip_client", "F5BigIpClient")(url=_https(u), username="u", password="p"), None, NAME),
    ("hpe_oneview", lambda u: _client("hpe_oneview_client", "HpeOneViewClient")(url=_https(u), username="u", password="p"), None, NAME),
    ("cisco_apic", lambda u: _client("cisco_apic_client", "CiscoApicClient")(url=_https(u), username="u", password="p"), None, NAME),
    ("qlik_sense_onprem", lambda u: _client("qlik_sense_onprem_client", "QlikSenseOnPremClient")(server_url=_https(u), client_cert="c", client_key="k"), None, NAME),
    ("vcenter", lambda u: _client("vcenter_client", "VCenterClient")(host=NAME, username="u", password="p"), _open_rest_session, NAME),
    # Vendor hosts.
    ("azure_cost", lambda u: _client("azure_cost_client", "AzureCostClient")(scope="subscriptions/s"), _azure_cost_request, "management.azure.com"),
    ("monday", lambda u: _client("monday_client", "MondayClient")(api_token="k"), None, "api.monday.com"),
    ("powerbi", lambda u: _client("powerbi_client", "PowerBIClient")(tenant_id="t", client_id="i", client_secret="s"), None, "login.microsoftonline.com"),
    ("netsuite", lambda u: _client("netsuite_client", "NetsuiteClient")(account_id="a", consumer_key="k", consumer_secret="s", token_id="t", token_secret="x"), None, "a.suitetalk.api.netsuite.com"),
]


def _row(case):
    name, build, probe, fixed = (*case, None, None)[:4]
    return pytest.param(build, probe or (lambda c: c.test_connection()), fixed, id=name)


ROWS = [_row(c) for c in CASES]


def _no_broker(**kwargs):
    raise ConnectionError("no broker in this test")


@pytest.fixture
def net(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(egress, "block_private_outbound", lambda: False)
    # Pinot's own broker driver is a separate path that this file does not cover.
    monkeypatch.setattr("app.data_sources.clients.pinot_client.pinot_connect", _no_broker)
    trap = NetTrap(monkeypatch, {})
    try:
        yield trap
    finally:
        trap.close()


def _run(probe, client):
    """Connectors report trouble in their own ways; only the wire matters here."""
    try:
        probe(client)
    except Exception:
        pass


@pytest.mark.parametrize("build,probe,fixed", ROWS)
def test_name_that_changes_follows_the_outbound_policy(net, build, probe, fixed):
    routes = _Anything()
    with local_server(routes) as (port, seen):
        net.table[fixed or NAME] = ["127.0.0.1"]
        client = build(f"http://{NAME}:{port}")
        if not fixed:
            _run(probe, client)
            assert seen, "the connector never reached the server"
        net.table[fixed or NAME] = ["169.254.169.254"]
        _run(probe, client)
    assert net.hits == []


@pytest.mark.parametrize("build,probe,fixed", [r for r in ROWS if not r.values[2]])
def test_redirect_follows_the_outbound_policy(net, build, probe, fixed):
    routes = _Anything()
    with local_server(routes) as (port, seen):
        net.table[NAME] = ["127.0.0.1"]
        client = build(f"http://{NAME}:{port}")
        _run(probe, client)
        assert seen, "the connector never reached the server"
        routes.redirect = True
        _run(probe, client)
    assert net.hits == []


_REQUESTS_CALLABLES = {"Session", "session", "get", "post", "put", "patch", "delete", "head", "options", "request"}


def _uses_requests(tree):
    """Does this module make HTTP calls through ``requests`` or the pinned helpers?"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "requests" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "requests":
                return True
            if node.module == "app.core.egress" and any(a.name in ("pinned_session", "pinned_request") for a in node.names):
                return True
    return False


def _mounts_pinned_adapter(function):
    return any(isinstance(n, ast.Name) and n.id == "PinnedHTTPAdapter" for n in ast.walk(function))


def _unpinned_calls(tree):
    """Line numbers of calls that go straight to ``requests``, however it was imported.

    A ``Session()`` built in a function that also mounts ``PinnedHTTPAdapter``
    is pinned, so it is not reported.
    """
    module_names, callable_names = set(), {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_names |= {a.asname or a.name for a in node.names if a.name == "requests"}
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "requests":
            callable_names |= {a.asname or a.name: a.name for a in node.names if a.name in _REQUESTS_CALLABLES}

    def called(call):
        func = call.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in module_names:
            return func.attr if func.attr in _REQUESTS_CALLABLES else None
        return callable_names.get(func.id) if isinstance(func, ast.Name) else None

    pinned_scopes = [f for f in ast.walk(tree)
                     if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and _mounts_pinned_adapter(f)]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = called(node)
        if name is None:
            continue
        if name in ("Session", "session") and any(node in ast.walk(f) for f in pinned_scopes):
            continue
        yield node.lineno


@pytest.mark.parametrize("path", sorted(CLIENTS_DIR.glob("*.py")), ids=lambda p: p.stem)
def test_no_connector_calls_requests_directly(path):
    """Covers the code paths a ``test_connection`` probe does not reach."""
    assert list(_unpinned_calls(ast.parse(path.read_text()))) == []


@pytest.mark.parametrize("source,flagged", [
    ("import requests\nrequests.get('u')", True),
    ("import requests\ns = requests.Session()", True),
    ("import requests as r\nr.post('u')", True),
    ("import requests as r\nr.Session()", True),
    ("from requests import get\nget('u')", True),
    ("from requests import Session as S\nS()", True),
    ("from requests.sessions import Session\nSession()", True),
    ("import requests\nraise requests.exceptions.ConnectionError()", False),
    ("from app.core.egress import pinned_session\npinned_session()", False),
    ("import requests\nfrom app.core.egress import PinnedHTTPAdapter\n"
     "def build():\n    s = requests.Session()\n    s.mount('https://', PinnedHTTPAdapter())\n    return s", False),
])
def test_the_direct_call_check_sees_every_import_form(source, flagged):
    assert bool(list(_unpinned_calls(ast.parse(source)))) is flagged


# Modules that share their HTTP code with connectors that have their own row.
_COVERED_THROUGH_SUBCLASSES = {"xmla_base"}  # analysis_services, sap_bw_xmla, infor_olap


def test_every_connector_on_requests_has_a_row_in_the_matrix():
    """A connector added without a row fails here; the row then proves it is pinned."""
    on_requests = {p.stem.removesuffix("_client") for p in CLIENTS_DIR.glob("*.py")
                   if _uses_requests(ast.parse(p.read_text()))}
    assert on_requests - {c[0] for c in CASES} - _COVERED_THROUGH_SUBCLASSES == set()


def test_a_pinned_session_keeps_requests_auth_retries_on_the_same_transport():
    """NTLM and Kerberos handlers re-send through the adapter."""
    requests_ntlm = pytest.importorskip("requests_ntlm")
    tokens = []

    def challenge(handler):
        tokens.append(handler.headers.get("Authorization"))
        return b""

    routes = {"/": (401, {"WWW-Authenticate": "NTLM"}, challenge)}
    with local_server(routes) as (port, seen):
        with pinned_session() as s:
            s.trust_env = False
            s.auth = requests_ntlm.HttpNtlmAuth("corp\\u", "p")
            # The fake server sends no real challenge, so the handler stops after
            # its first answer; what matters is that the answer went out.
            with pytest.raises(PermissionError):
                s.get(f"http://127.0.0.1:{port}/", timeout=5)
    assert tokens[0] is None
    assert tokens[1].startswith("NTLM ")


def test_session_settings_made_after_creation_still_apply(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    with local_server() as (port, seen):
        with pinned_session() as s:
            s.trust_env = False
            assert s.get(f"http://127.0.0.1:{port}/direct", timeout=5).status_code == 200
    assert seen[0][1] == "/direct"


def test_one_shot_request_follows_the_session_contract(net):
    net.table[NAME] = ["169.254.169.254"]
    with pytest.raises(requests.exceptions.ConnectionError):
        pinned_request("GET", f"http://{NAME}/", timeout=5)
    assert net.hits == []


def test_a_name_that_does_not_resolve_is_not_reported_as_refused(net, monkeypatch):
    def nothing(host, *a, **k):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(egress, "_getaddrinfo", nothing)
    with pytest.raises(requests.exceptions.ConnectionError) as exc:
        pinned_request("GET", f"http://{NAME}/", timeout=5)
    assert not isinstance(exc.value, egress.UnsafeURLError)


def test_a_copied_adapter_keeps_its_policy(net):
    import pickle

    def session_with(policy):
        restored = pickle.loads(pickle.dumps(PinnedHTTPAdapter(policy)))
        session = requests.Session()
        session.mount("http://", restored)
        return session

    with local_server() as (port, seen):
        net.table[NAME] = ["127.0.0.1"]
        with session_with("admin") as s:
            assert s.get(f"http://{NAME}:{port}/allowed", timeout=5).status_code == 200
        with session_with("public") as s, pytest.raises(egress.UnsafeURLError):
            s.get(f"http://{NAME}:{port}/refused", timeout=5)
    assert [path for _, path, _ in seen] == ["/allowed"]

    net.table[NAME] = ["169.254.169.254"]
    with session_with("admin") as s, pytest.raises(egress.UnsafeURLError):
        s.get(f"http://{NAME}:{port}/", timeout=5)
    assert net.hits == []
